"""Dictation through Claude Code's own voice mode.

Claude Code streams a microphone to Anthropic's speech service, but only from a local audio device. The server has none, so a virtual
microphone (infra/95-agent-pulse.sh) stands in: the browser's audio is played into it while this module holds the push-to-talk key
of the chat's CLI (hold mode, rebound to meta+k so that recording starts on the first key press). The transcript appears in the CLI prompt;
it is read from the screen, cleared there and handed to the browser.
"""
import asyncio
import json
import os
import re
import time

from fastapi import WebSocketDisconnect

from . import cli_controls, config, tmuxctl

PLAYER = os.environ.get("PORTAL_PAPLAY", "paplay")
SINK = "vmic_sink"
KEY = "M-k"
RATE = 16000
TAIL = RATE * 2 // 2
MAX_SECONDS = 180
# While it records, the CLI draws its microphone level as a block character at the cursor.
LEVEL = re.compile("[\u2581-\u2588]+$")
RULE = re.compile(r"^\s*[─━═]{5,}\s*$")

_active = {"name": None}


def prompt_text(screen):
    """Text of the CLI prompt box: the lines after the last prompt marker up to the next rule."""
    lines = screen.rstrip().splitlines()
    marks = [i for i, line in enumerate(lines) if re.match(r"^\s*❯", line)]
    if not marks:
        return None
    first = marks[-1]
    parts = [re.sub(r"^\s*❯\s?", "", lines[first]).strip()]
    for line in lines[first + 1:]:
        if RULE.match(line) or not line.startswith("  "):
            break
        parts.append(line.strip())
    return LEVEL.sub("", " ".join(part for part in parts if part)).strip()


def absorb(whole, visible):
    """The prompt box shows only its last few lines; fold what is visible into the text collected so far."""
    if not visible:
        return whole
    if not whole or visible.startswith(whole):
        return visible
    if visible in whole:
        return whole
    anchor = " ".join(visible.split()[:3])
    at = whole.rfind(anchor) if len(anchor) >= 10 else -1
    if at >= 0:
        return whole[:at] + visible
    return whole + " " + visible


def listening(screen):
    return bool(re.search(r"listening|keep holding", screen))


async def tmux_keys(name, *keys):
    await tmuxctl.run("send-keys", "-t", f"={name}:", *keys)


async def hold(name):
    # Claude Code recognises a held key by its repeats; they stop when the key is "released".
    while True:
        await tmux_keys(name, KEY)
        await asyncio.sleep(0.05)


async def clear(name):
    for _ in range(4):
        await tmux_keys(name, "C-u")
        await asyncio.sleep(0.15)
        if prompt_text(await cli_controls.capture(name)) == "":
            return


async def settled(name, whole="", silence=1.4, limit=10.0):
    started = last_change = time.monotonic()
    text = None
    while time.monotonic() - started < limit:
        await asyncio.sleep(0.2)
        screen = await cli_controls.capture(name)
        now = prompt_text(screen)
        if now != text:
            text, last_change = now, time.monotonic()
            whole = absorb(whole, now)
        elif time.monotonic() - last_change >= silence and not listening(screen) and time.monotonic() - started > 1.0:
            break
    return whole


async def player():
    return await asyncio.create_subprocess_exec(
        PLAYER, f"--server=unix:{config.PULSE_SOCKET}", f"--device={SINK}", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=40",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=tmuxctl.child_env())


async def ready_check(name):
    if not os.path.exists(config.PULSE_SOCKET):
        return "Dictation is not set up on this server"
    try:
        await tmuxctl.require_session(name)
        _, names = await tmuxctl.claude_count()
    except tmuxctl.TmuxError:
        return "No running chat"
    if name not in names or cli_controls.busy(name):
        return "Claude is not ready"
    try:
        screen = await cli_controls.capture(name)
    except tmuxctl.TmuxError:
        return "Dictation is not available in this chat"
    if not cli_controls.idle(screen):
        return "Claude must be idle with an empty prompt"
    if prompt_text(screen):
        return "Clear the Claude prompt first"
    return None


async def serve(ws, name):
    await ws.accept()

    async def send(**message):
        await ws.send_text(json.dumps(message))

    if _active["name"] is not None:
        await send(type="busy", reason="Another dictation is running")
        await ws.close()
        return
    _active["name"] = name
    repeat = proc = watcher = None
    try:
        problem = await ready_check(name)
        if problem:
            await send(type="unavailable", reason=problem)
            return
        first = await ws.receive_text()
        if json.loads(first).get("type") != "start":
            return
        repeat = asyncio.create_task(hold(name))
        for _ in range(60):
            if listening(await cli_controls.capture(name)):
                break
            await asyncio.sleep(0.05)
        else:
            repeat.cancel()
            repeat = None
            await tmux_keys(name, "C-u")
            await send(type="unavailable", reason="Voice mode is not enabled in this chat; open it again or start a new chat")
            return
        proc = await player()
        await send(type="ready")

        heard = {"text": ""}

        async def partials():
            while True:
                await asyncio.sleep(0.25)
                whole = absorb(heard["text"], prompt_text(await cli_controls.capture(name)))
                if whole != heard["text"]:
                    heard["text"] = whole
                    await send(type="partial", text=whole)

        watcher = asyncio.create_task(partials())
        started = time.monotonic()
        cancelled = False
        while time.monotonic() - started < MAX_SECONDS:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                cancelled = True
                break
            if message.get("bytes"):
                proc.stdin.write(message["bytes"])
                await proc.stdin.drain()
            elif message.get("text"):
                kind = json.loads(message["text"]).get("type")
                if kind in ("stop", "cancel"):
                    cancelled = kind == "cancel"
                    break
        watcher.cancel()
        watcher = None
        if cancelled:
            repeat.cancel()
            repeat = None
            await tmux_keys(name, "Escape")
            await asyncio.sleep(0.3)
            await clear(name)
            return
        proc.stdin.write(b"\0" * TAIL)
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 20)
        repeat.cancel()
        repeat = None
        text = await settled(name, heard["text"])
        await clear(name)
        await send(type="final", text=text)
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, OSError, ValueError, RuntimeError, AttributeError, TypeError, tmuxctl.TmuxError):
        try:
            await send(type="error", reason="Dictation failed")
        except Exception:
            pass
    finally:
        for task in (repeat, watcher):
            if task:
                task.cancel()
        if proc and proc.returncode is None:
            proc.kill()
        _active["name"] = None
        try:
            await ws.close()
        except Exception:
            pass

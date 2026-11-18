"""WebSocket of an agent-mode chat: the same events the terminal bridge gives the chat view, without a terminal.

Server to client: {"v": 1, "type": "code_events", "events": [...]}, with transcript_ready (the saved reply is ready to fetch),
terminal_preview (the reply as it is being written) and agent_state (working, waiting for an answer, stopped).
Client to server: {"type": "message", "text": "..."}.
"""
import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from . import agentctl, chat_runtime, config, identity
from .errors import TmuxError

MAX_MESSAGE = 16372
PREVIEW_EVERY = 0.08
_active = 0


def valid_text(text):
    if not isinstance(text, str) or not text.strip():
        return False
    if any((ord(ch) < 32 and ch not in "\n\t") or 127 <= ord(ch) <= 159 for ch in text):
        return False
    return len(text.encode("utf-8")) <= MAX_MESSAGE


def state_event(chat):
    return {"type": "agent_state", "sid": chat.session_id or chat.meta.get("sid"), "status": chat.status(), "alive": chat.alive,
            "prompt": bool(chat.requests)}


async def bridge(ws: WebSocket, name: str) -> None:
    global _active
    chat = agentctl.hub.get(name)
    if chat is None or chat not in agentctl.hub.visible():
        await ws.close(code=1008)
        return
    if _active >= config.MAX_TERM_CLIENTS:
        await ws.close(code=1013)
        return
    _active += 1
    sub = chat.subscribe()
    try:
        await ws.accept()
        await ws.send_json({"v": 1, "type": "code_events", "events": [state_event(chat), {"type": "transcript_ready", "sid": chat.session_id, "turn_id": chat.turn_id}]})
        shown = chat.preview_version

        async def pump_out():
            nonlocal shown
            while True:
                try:
                    await asyncio.wait_for(sub.wake.wait(), 1.0)
                except asyncio.TimeoutError:
                    continue
                sub.wake.clear()
                events = []
                while sub.events:
                    events.append(sub.events.popleft())
                if events:
                    events.append(state_event(chat))
                    await ws.send_json({"v": 1, "type": "code_events", "events": events})
                if chat.preview_version != shown and chat.preview and chat.busy:
                    shown = chat.preview_version
                    await ws.send_json({"v": 1, "type": "code_events", "events": [chat.preview]})
                    await asyncio.sleep(PREVIEW_EVERY)
                    sub.wake.set()

        async def pump_in():
            while True:
                incoming = await ws.receive()
                if not ws.scope.get("portal_valid", lambda: True)() or incoming["type"] == "websocket.disconnect":
                    return
                text = incoming.get("text")
                if text is None:
                    continue
                if len(text) > 70000:
                    return
                try:
                    request = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(request, dict) or request.get("type") != "message":
                    continue
                body = request.get("text")
                if not valid_text(body):
                    await ws.send_json({"v": 1, "type": "code_events", "events": [{"type": "agent_error", "sid": chat.session_id, "detail": "Unsupported message"}]})
                    continue
                try:
                    await chat.say(identity.neutralize(body.replace("\r\n", "\n").replace("\r", "\n")))
                except TmuxError as error:
                    await ws.send_json({"v": 1, "type": "code_events", "events": [{"type": "agent_error", "sid": chat.session_id, "detail": error.detail}]})

        async def validate_connection():
            while ws.scope.get("portal_valid", lambda: True)():
                await asyncio.sleep(2)

        tasks = [asyncio.create_task(t()) for t in (pump_out, pump_in, validate_connection)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        _active -= 1
        chat.unsubscribe(sub)
        try:
            await ws.close()
        except Exception:
            pass

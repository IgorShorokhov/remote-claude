import asyncio
import re

from . import models, tmuxctl
from .cli_preview_rules import working_line
from .tmuxctl import TmuxError

_busy = set()


def busy(name):
    return name in _busy


async def capture(name):
    rc, screen = await tmuxctl.run('capture-pane', '-e', '-p', '-t', f'={name}:')
    if rc or len(screen) > 262144:
        raise TmuxError(409, 'Could not read Claude screen')
    return plain(screen)


HINT = re.compile(r'^((?:[ \t\xa0]|\x1b\[[0-9;]*m)*❯[ \t\xa0]*(?:\x1b\[[0-9;]*m)*)\x1b\[2m[^\x1b\n]*(?:\x1b\[(?:0|22)?m)?[ \t\xa0]*$', re.M)
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def plain(screen):
    return ANSI.sub('', HINT.sub(r'\1', screen))


async def key(name, value, literal=False):
    args = ('-l', value) if literal else (value,)
    rc, _ = await tmuxctl.run('send-keys', '-t', f'={name}:', *args)
    if rc:
        raise TmuxError(409, 'Command not delivered; check the console')
    await asyncio.sleep(.12)


async def menu(name):
    if not idle(await capture(name)):
        raise TmuxError(409, 'Claude must be idle with an empty prompt; open the console')
    await key(name, '/model', literal=True)
    await key(name, 'Enter')
    for _ in range(16):
        try:
            return parse_picker(await capture(name))
        except TmuxError:
            await asyncio.sleep(.12)
    if 'Select model' in await capture(name):
        await key(name, 'Escape')
    raise TmuxError(409, 'Claude did not open the menu; check the console')


async def type_model(name, model):
    # The menu lists Opus Plan Mode only while it is the active model, so the first switch to it is typed.
    await key(name, 'Escape')
    for _ in range(16):
        if idle(await capture(name)):
            break
        await asyncio.sleep(.12)
    else:
        raise TmuxError(409, 'Claude did not close the menu; check the console')
    await key(name, f'/model {model}', literal=True)
    await key(name, 'Enter')
    for _ in range(8):
        await asyncio.sleep(.4)
        screen = await capture(name)
        reason = stuck(screen)
        if reason:
            raise TmuxError(409, reason)
        if not idle(screen):
            continue
        data = await menu(name)
        if data['checked'] == model:
            return data
        await key(name, 'Escape')
    raise TmuxError(409, 'Claude did not confirm Opus Plan Mode; check the console')


async def confirm_switch(name, label):
    for _ in range(8):
        screen = await capture(name)
        if 'Switch model?' not in screen:
            return
        if re.search(r'❯[ \t\xa0]*1\.[ \t\xa0]+Yes, switch to ' + re.escape(label) + r'\b', screen):
            await key(name, 'Enter')
            await asyncio.sleep(.4)
            continue
        await key(name, 'Escape')
        raise TmuxError(409, f'Claude asked to confirm another model than {label}; check the console')
    raise TmuxError(409, 'Claude kept asking to confirm the switch; check the console')


async def compact(name, instructions=''):
    async with tmuxctl._lock:
        await tmuxctl.require_session(name)
        _, names = await tmuxctl.claude_count()
        if name not in names or len((await tmuxctl.pane_pids()).get(name, [])) != 1:
            raise TmuxError(409, 'Needs a single-pane Claude session')
        if not idle(await capture(name)):
            raise TmuxError(409, 'Claude must be idle with an empty prompt')
        await key(name, '/compact' + (' ' + instructions if instructions else ''), literal=True)
        await key(name, 'Enter')


async def complete_menu(name, data):
    original = data['selected']
    checked = data['checked']
    rows = {row['position']: row for row in data['models']}
    total = data['total']
    for direction in ('Up', 'Down'):
        visited = set()
        for _ in range(30):
            if data['selected'] in visited:
                break
            visited.add(data['selected'])
            await key(name, direction)
            data = parse_picker(await capture(name))
            rows.update((row['position'], row) for row in data['models'])
            total = max(total, data['total'])
        else:
            raise TmuxError(409, 'Model menu too large; check the console')
    for _ in range(30):
        if data['selected'] == original:
            break
        current = next(row['position'] for row in data['models'] if row['id'] == data['selected'])
        target = next(position for position, row in rows.items() if row['id'] == original)
        await key(name, 'Up' if current > target else 'Down')
        data = parse_picker(await capture(name))
    if data['selected'] != original or data['checked'] != checked:
        raise TmuxError(409, 'Model menu changed during readback; retry')
    if total > 30 or sorted(rows) != list(range(1, total + 1)):
        raise TmuxError(409, 'Model menu incomplete; enlarge the console and retry')
    return {**data, 'models': [rows[position] for position in sorted(rows)], 'total': total}


async def remember(name, data):
    model = data['checked']
    if not model:
        raise TmuxError(409, 'Claude did not confirm the active model; check the console')
    label = next(row['label'] for row in data['models'] if row['id'] == model)
    if model == 'default':
        model = models.from_label(label)
        if not model:
            raise TmuxError(409, 'Claude did not name the active model')
    for key_name, value in (('@portal_model_state', 'readback_pending'), ('@portal_model', model), ('@portal_effort', data['effort'] or ''), ('@portal_model_state', 'cli_confirmed')):
        rc, _ = await tmuxctl.run('set-option', '-t', f'={name}:', key_name, value)
        if rc:
            raise TmuxError(409, 'CLI readback could not be saved; check the console')
    from . import chat_models
    await chat_models.remember_session(name, confirmed=True)
    return {**data, 'model': model, 'model_state': 'cli_confirmed', 'cli_model': label}


async def operate(name, model=None, effort=None):
    async with tmuxctl._lock:
        await tmuxctl.require_session(name)
        _, names = await tmuxctl.claude_count()
        if name not in names or len((await tmuxctl.pane_pids()).get(name, [])) != 1:
            raise TmuxError(409, 'Needs a single-pane Claude session')
        _busy.add(name)
        opened = False
        switch_started = False
        try:
            data = await menu(name)
            opened = True
            if model is None:
                return await remember(name, await complete_menu(name, data))
            if model == models.OPUSPLAN and data['checked'] != model:
                switch_started = True
                data = await type_model(name, model)
                opened = True
            target = next((row for row in data['models'] if row['id'] == model), None)
            for direction in ('Down', 'Up'):
                last = None
                for _ in range(30):
                    if target is not None or data['selected'] == last:
                        break
                    last = data['selected']
                    await key(name, direction)
                    data = parse_picker(await capture(name))
                    target = next((row for row in data['models'] if row['id'] == model), None)
            if target is None:
                raise TmuxError(409, f'{models.label(model)} is not in this Claude menu; update Claude Code')
            if not target['enabled']:
                raise TmuxError(409, f'{models.label(model)} is not available on this plan')
            switch_started = True
            for k, v in (('@portal_model', model), ('@portal_effort', effort or ''), ('@portal_model_state', 'switch_requested')):
                rc, _ = await tmuxctl.run('set-option', '-t', f'={name}:', k, v)
                if rc:
                    raise TmuxError(409, 'Could not save request')
            for _ in range(12):
                if data['selected'] == model:
                    break
                current = next(row['position'] for row in data['models'] if row['id'] == data['selected'])
                await key(name, 'Down' if current < target['position'] else 'Up')
                data = parse_picker(await capture(name))
            if data['selected'] != model:
                raise TmuxError(409, 'Claude did not select the model')
            adjust_effort = effort is not None and data['effort'] is not None
            effort = effort if adjust_effort else data['effort']
            if adjust_effort:
                for _ in range(6):
                    if data['effort'] == 'low':
                        break
                    await key(name, 'Left')
                    data = parse_picker(await capture(name))
                for _ in range(('low', 'medium', 'high', 'xhigh', 'max').index(effort)):
                    await key(name, 'Right')
            data = parse_picker(await capture(name))
            if data['selected'] != model or data['effort'] != effort:
                raise TmuxError(409, 'Claude rejected the effort level')
            await key(name, 's')
            opened = False
            await asyncio.sleep(.25)
            await confirm_switch(name, target['label'])
            verified = await menu(name)
            opened = True
            if verified['checked'] != model or verified['effort'] != effort:
                raise TmuxError(409, 'Claude did not confirm; check the console')
            return await remember(name, await complete_menu(name, verified))
        except TmuxError as e:
            setattr(e, 'model_unchanged', not switch_started)
            raise
        finally:
            try:
                if opened and 'Select model' in await capture(name):
                    await key(name, 'Escape')
            finally:
                _busy.discard(name)

ROW = re.compile(r'^[ \t\xa0]*(?:(❯)|[↑↓])?[ \t\xa0]*(\d+)\.[ \t\xa0]+(Default|Opus|Fable|Sonnet|Haiku)\b(.*)$', re.M)
EFFORT = re.compile(r'\b(Low|Medium|High|Xhigh|Max) effort(?: \([^)\n]*\))?\s+←/→ to adjust', re.I)
VERSION = re.compile(r'\d[\w.]*')


def parse_picker(screen):
    if 'Select model' not in screen or not re.search(r's to use this\s+session only', screen):
        raise TmuxError(409, 'Model menu not recognized; open the console')
    rows = []
    selected = checked = None
    for match in ROW.finditer(re.sub(r'\(recommend[^)\s]*\)?…?', '', screen)):
        current, number, alias, rest = match.groups()
        parts = [p for p in re.split(r'\s{2,}', rest.replace('✔', '  ').strip()) if p]
        if not parts:
            continue
        label = alias + ' ' + parts[0] if VERSION.fullmatch(parts[0]) else re.split(r'\s*·', parts[0])[0].strip()
        if alias == 'Opus' and label == 'Plan Mode':
            label = 'Opus Plan Mode'
        identity = 'default' if alias == 'Default' else models.from_label(label) or alias.lower()
        enabled = not re.search(r'requires|unavailable|not available|not included|disabled', rest, re.I)
        rows.append({'id': identity, 'label': label, 'position': int(number), 'enabled': enabled})
        if current:
            selected = identity
        if '✔' in rest:
            checked = identity
    effort = EFFORT.search(screen)
    if not rows or selected is None or len({r['position'] for r in rows}) != len(rows):
        raise TmuxError(409, 'Model menu cut off; enlarge the console and retry')
    hidden = re.search(r'…\s*\+(\d+)\s+models', screen)
    total = max(row['position'] for row in rows) + (int(hidden[1]) if hidden else 0)
    return {'models': rows, 'selected': selected, 'checked': checked, 'effort': effort[1].lower() if effort else None, 'total': total, 'source': 'claude_cli_picker'}


def stuck(screen):
    if re.search(r'uses usage credits|don.t have usage credits', screen):
        return 'Model needs usage credits; pick another model'
    if re.search(r"issue with the selected model|isn't recognized", screen):
        return 'Claude Code rejected this model id'
    return None


def idle(screen):
    lines = screen.rstrip().splitlines()
    prompts = [i for i, line in enumerate(lines) if re.fullmatch(r'\s*❯\s*', line)]
    if not prompts:
        return False
    at = prompts[-1]
    if at < len(lines) - 6 or at < 1 or at + 1 >= len(lines):
        return False
    if not all(re.fullmatch(r'\s*─{5,}\s*', lines[n]) for n in (at - 1, at + 1)):
        return False
    return not any(working_line(line) for line in lines[max(0, at - 8):at]) and not re.search(r'esc to interrupt|esc to cancel', '\n'.join(lines[max(0, at - 8):]), re.I) and not re.search(r'Do you want|Enter to confirm|Select model', '\n'.join(lines[at:]), re.I)

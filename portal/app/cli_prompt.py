import hashlib
import json
import re

from . import cli_controls, tmuxctl
from .tmuxctl import TmuxError

FOOT = re.compile(r'(?i)\besc to cancel\b')
OPTION = re.compile(r'^\s*(❯\s*)?(\d{1,2})\.\s+(\S.*)$')
RULE = re.compile(r'^\s*[─━]{5,}\s*$')
SEPARATOR = re.compile(r'^\s*╌{3,}\s*$')
TICK = re.compile(r'^\[[ xX✓✔]?\]\s')
TABS = re.compile(r'^(?:←\s*)?(?:[☐☑✔✓]\s*)?')
TAIL = re.compile(r'\s*(?:\(shift\+tab\)|✔ Submit\s*→?|→)\s*$')
LIMIT = 6000

_answering = set()


def _head(rows):
    out, tip = [], False
    for row in rows:
        text = row.strip()
        if not text or SEPARATOR.match(row):
            tip = False
            continue
        if text.startswith('Tip:'):
            tip = True
            continue
        if tip and text[0].islower():
            continue
        tip = False
        out.append(text[1:].strip() if text.startswith('│') else text)
    return out


def parse(screen):
    rows = [line.replace('\xa0', ' ').rstrip() for line in screen.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    if not rows or not FOOT.search(rows[-1]):
        return None
    foot = len(rows) - 1
    first = next((y for y in range(foot - 1, -1, -1) if (m := OPTION.match(rows[y])) and m[2] == '1'), None)
    if first is None:
        return None
    top = next((y for y in range(first - 1, -1, -1) if RULE.match(rows[y])), -1)
    head = _head(rows[top + 1:first])
    options, selected = [], None
    for row in rows[first:foot]:
        if RULE.match(row) or not row:
            continue
        match = OPTION.match(row)
        if match and int(match[2]) == len(options) + 1:
            if match[1]:
                selected = len(options) + 1
            options.append({'n': len(options) + 1, 'label': match[3], 'hint': ''})
        elif options:
            options[-1]['hint'] = (options[-1]['hint'] + ' ' + row.strip()).strip()
    if selected is None:
        return None
    permission = 'Tab to amend' in rows[foot] or any(line.startswith('Do you want') for line in head[-1:])
    for option in options:
        if permission and option['hint']:
            option['label'] += ' ' + option['hint']
            option['hint'] = ''
        option['label'] = TAIL.sub('', option['label'])
        option['free'] = bool(re.match(r'(?i)type something', option['label']))
    title = TAIL.sub('', TABS.sub('', head[0])) if head else ''
    question = head[-1] if len(head) > 1 else ''
    detail = '\n'.join(head[1:-1])[:LIMIT]
    seen = json.dumps([title, detail, question, [option['label'] for option in options]], ensure_ascii=False)
    return {'signature': hashlib.sha256(seen.encode()).hexdigest()[:16], 'title': title, 'detail': detail, 'question': question,
            'options': options, 'selected': selected, 'permission': permission,
            'multiple': any(TICK.match(option['label']) for option in options)}


async def current(name):
    await tmuxctl.require_session(name)
    if cli_controls.busy(name) or name in _answering:
        return None
    return parse(await cli_controls.capture(name))


async def answer(name, signature, option=None, text=None):
    await tmuxctl.require_session(name)
    if cli_controls.busy(name) or name in _answering:
        raise TmuxError(409, 'Another answer is being delivered')
    _answering.add(name)
    try:
        dialog = parse(await cli_controls.capture(name))
        if not dialog or dialog['signature'] != signature:
            raise TmuxError(409, 'This question is no longer open')
        if option is None:
            await cli_controls.key(name, 'Escape')
            return
        chosen = next((item for item in dialog['options'] if item['n'] == option), None)
        if chosen is None or dialog['multiple'] or bool(text) != chosen['free']:
            raise TmuxError(400, 'This answer does not fit the question')
        for _ in range(abs(option - dialog['selected'])):
            await cli_controls.key(name, 'Down' if option > dialog['selected'] else 'Up')
        moved = parse(await cli_controls.capture(name))
        if not moved or moved['signature'] != signature or moved['selected'] != option:
            raise TmuxError(409, 'Could not select this answer; check the console')
        if text:
            await cli_controls.key(name, ' '.join(text.split()), literal=True)
        await cli_controls.key(name, 'Enter')
    finally:
        _answering.discard(name)

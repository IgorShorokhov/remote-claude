import json
import os
import re
import stat
from pathlib import Path

from . import config

MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,190}(?:\[1m\])?")
LATEST = {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5', 'haiku': 'claude-haiku-5-5', 'fable': 'claude-fable-5-2'}
ALIASES = tuple(LATEST.values()) + ('claude-fable-5-1',)
OPUSPLAN = 'opusplan'
NOTES = {'claude-fable-5-2': 'Pro launch offer until Nov 16, then usage credits', 'claude-fable-5-1': 'Needs usage credits', OPUSPLAN: 'Opus in plan mode, Sonnet otherwise'}
FULL_RE = re.compile(r'claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?')
LABEL_RE = re.compile(r'(Opus|Sonnet|Haiku|Fable) (\d+)(?:\.(\d+))?')
MANAGED_SETTINGS = Path('/etc/claude-code/managed-settings.json')
ENV_KEYS = ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL', 'ANTHROPIC_CUSTOM_MODEL_OPTION',
            'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_FABLE_MODEL')


def valid(value):
    return isinstance(value, str) and MODEL_RE.fullmatch(value) is not None and not value.lower().startswith(('sk-', 'token', 'password'))


def canonical(value):
    return LATEST.get(value, value) if isinstance(value, str) else value


def label(value):
    if value in (None, 'default'):
        return 'Model not saved'
    if value == OPUSPLAN:
        return 'Opus Plan Mode'
    match = FULL_RE.fullmatch(canonical(value) or '')
    if not match:
        return value
    family, major, minor = match.groups()
    return family.capitalize() + ' ' + major + ('.' + minor if minor else '')


def from_label(text):
    if text == 'Opus Plan Mode':
        return OPUSPLAN
    match = LABEL_RE.fullmatch(text or '')
    if not match:
        return None
    family, major, minor = match.groups()
    return 'claude-' + family.lower() + '-' + major + ('-' + minor if minor else '')


def runs_under_opusplan(value):
    match = FULL_RE.fullmatch(canonical(value) or '')
    return match is not None and match[1] in ('opus', 'sonnet')


def settings(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 262144:
                return {}
            data = json.loads(f.read(262145))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def catalog():
    user = settings(config.HOME / '.claude/settings.json')
    managed = settings(MANAGED_SETTINGS)
    environment = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    rows = {}

    def add(value, source):
        value = canonical(value)
        if valid(value) and value != 'default':
            rows.setdefault(value, {'id': value, 'label': label(value), 'source': source, **({'note': NOTES[value]} if value in NOTES else {})})

    for alias in ALIASES + (OPUSPLAN,):
        add(alias, 'alias')
    for data, source in ((user, 'user_settings'), (managed, 'managed_settings')):
        if isinstance(data.get('env'), dict):
            environment.update({k: data['env'][k] for k in ENV_KEYS if k in data['env']})
        add(data.get('model'), source)
        for value in (data.get('availableModels') or []) if isinstance(data.get('availableModels'), list) else []:
            add(value, source)
    for key in ENV_KEYS:
        add(environment.get(key), 'environment')
    restriction = managed.get('availableModels', user.get('availableModels'))
    if isinstance(restriction, list):
        allowed = {canonical(v) for v in restriction if valid(v)}
        rows = {k: v for k, v in rows.items() if k in allowed}
    default = environment.get('ANTHROPIC_MODEL') or managed.get('model') or user.get('model') or environment.get('ANTHROPIC_DEFAULT_MODEL')
    return {'models': list(rows.values()), 'preferred': LATEST['sonnet'],
            'default': default if valid(default) else None, 'verified': False,
            'note': 'Configured options only. Check the active model with /status.'}


def validate(value):
    if value is None:
        return None
    value = canonical(value)
    if not valid(value) or value not in {m['id'] for m in catalog()['models']}:
        from .tmuxctl import TmuxError
        raise TmuxError(400, 'Model not in configured list')
    return value

"""Automatic model and effort for a new Code Web chat ("Auto").

Google's Decision model (gemini-decision-1) writes no text: for typed questions about an input it returns probabilities.
The first message of a new chat is the input, two multiple-choice questions ask which Claude model and which effort fit it,
and the answer is stored once in the chat. Any failure falls back to Sonnet at medium effort, so sending a message waits
for Google a few seconds at most. Every account's new chats are routed this way while a key is set; the help text of the
model menu says that the first message goes to Google.
"""
import logging
import os
import re
import stat
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import models
from .backup_agent import state_dir, write_private

log = logging.getLogger(__name__)

AUTO = 'auto'
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/decisions'
DECISION_MODEL = 'gemini-decision-1'
TEXT_LIMIT = 4000
MIN_CONFIDENCE = 0.35
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
FALLBACK_MODEL = 'sonnet'
FALLBACK_EFFORT = 'medium'
KEY_RE = re.compile(r'[\x21-\x7e]{8,4096}')

# Haiku cannot be trusted with pictures or documents: the request is refused in the choice and in pick() below.
VISION_RE = re.compile(r'(?i)(картин|скриншот|фото|изображ|скан|распозна|ocr|pdf|screenshot|image|photo|picture|diagram|'
                       r'\.(?:png|jpe?g|gif|webp|bmp|tiff?|heic|heif|svg|pdf)\b)')
MODEL_OPTIONS = {
    'haiku': 'A small, quick job: a short factual question, a one-line edit, a lookup, formatting, a simple shell command, casual conversation. '
             'Never Haiku when the job needs seeing or reading an image, screenshot, photo, scan, diagram or PDF, or reading text from any picture: choose Sonnet or Opus for those.',
    'sonnet': 'An everyday programming or study task of moderate size: writing or fixing a function or a script, explaining code or a concept, a typical edit in a few files, a document or a report.',
    'opus': 'A hard task: large or multi-file changes, architecture or design decisions, difficult debugging, a security review, deep maths or proofs, long research that needs careful multi-step reasoning.',
}
EFFORT_OPTIONS = {
    'low': 'A direct answer is enough; little or no deliberation is needed.',
    'medium': 'Some deliberation is needed, as for an ordinary task.',
    'high': 'Thorough step-by-step reasoning and checking of the result are needed.',
    'xhigh': 'The hardest requests: extensive step-by-step reasoning, many interdependent parts, and careful verification of every result.',
}
QUESTIONS = {
    'model': {'type': 'choice', 'criteria': MODEL_OPTIONS,
              'instructions': 'Which model is the right size for the request in the message? Prefer the smallest model that will do the job well, '
                              'but if the request needs to recognise images, read text from pictures or scans, or work with PDFs or other attached images, never pick haiku.'},
    'effort': {'type': 'choice', 'criteria': EFFORT_OPTIONS,
               'instructions': 'How much reasoning effort does the request in the message deserve?'},
}
INTRO = 'A person is starting a new chat with a coding assistant that works on their computer and server. This is their first message:\n\n'


class DecisionError(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def key_path() -> Path:
    override = os.environ.get('PORTAL_DECISION_KEY_FILE')
    return Path(override) if override else state_dir() / 'decision.key'


def read_key() -> str | None:
    try:
        fd = os.open(key_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600 or info.st_uid != os.geteuid():
                return None
            key = stream.read(4097).strip()
    except (OSError, ValueError, HTTPException):
        return None
    return key if KEY_RE.fullmatch(key) else None


def configured() -> bool:
    return read_key() is not None


def available() -> bool:
    """Auto is offered to every account while a key is set."""
    return configured()


def store_key(key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise ValueError('key format')
    write_private(key_path(), key)


def remove_key() -> None:
    key_path().unlink(missing_ok=True)


def call(key: str, text: str, questions: dict) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.post(ENDPOINT, headers={'x-goog-api-key': key},
                                   json={'model': DECISION_MODEL, 'input': text, 'questions': questions})
    except httpx.HTTPError:
        raise DecisionError('unreachable') from None
    if response.status_code == 200:
        try:
            answers = response.json()['answers']
        except (ValueError, KeyError, TypeError):
            raise DecisionError('format') from None
        if not isinstance(answers, dict):
            raise DecisionError('format')
        return answers
    message = ''
    try:
        error = response.json().get('error')
        message = str(error.get('message', '')) if isinstance(error, dict) else ''
    except (ValueError, AttributeError):
        pass
    if response.status_code in (401, 403) or (response.status_code == 400 and 'api key' in message.lower()):
        raise DecisionError('rejected')
    raise DecisionError('limit' if response.status_code == 429 else 'refused')


SAMPLE = 'Fix a typo in the README.'


def check_key(key: str) -> dict:
    """The real questions on a sample message: proves the key and shows what Auto would answer. Raises DecisionError."""
    answers = call(key, INTRO + SAMPLE, QUESTIONS)
    summary = {}
    for name in QUESTIONS:
        item = answers.get(name)
        if isinstance(item, dict) and isinstance(item.get('choice'), str):
            confidence = item.get('confidence')
            summary[name] = {'choice': item['choice'][:20], 'confidence': round(confidence, 2) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None}
    return summary


def _answer(answers: dict, name: str, options: dict) -> str | None:
    item = answers.get(name)
    if not isinstance(item, dict) or item.get('choice') not in options:
        return None
    confidence = item.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
        return None
    return item['choice']


def _full(alias: str) -> str:
    """The family's current model if the configured list offers it, otherwise Sonnet's."""
    for name in (alias, FALLBACK_MODEL):
        try:
            return models.validate(models.LATEST[name])
        except Exception:
            continue
    return models.LATEST[FALLBACK_MODEL]


def pick(text: str, allowed: bool = True) -> dict:
    """{'model': full model id, 'effort': low/medium/high/xhigh, 'source': 'decision' or 'default'}. Never raises."""
    alias, effort, source = FALLBACK_MODEL, FALLBACK_EFFORT, 'default'
    text = (text or '').strip()[:TEXT_LIMIT]
    key = read_key() if allowed and text else None
    if key:
        try:
            answers = call(key, INTRO + text, QUESTIONS)
        except DecisionError as error:
            log.warning('Auto model choice fell back to the default: %s', error.kind)
        else:
            chosen_model = _answer(answers, 'model', MODEL_OPTIONS)
            if chosen_model == 'haiku' and VISION_RE.search(text):
                chosen_model = 'sonnet'
            chosen_effort = _answer(answers, 'effort', EFFORT_OPTIONS)
            if chosen_model or chosen_effort:
                alias = chosen_model or alias
                effort = chosen_effort or effort
                source = 'decision'
    return {'model': _full(alias), 'effort': effort, 'source': source}

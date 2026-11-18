import base64
import binascii
import asyncio
import os
import stat
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from . import config, fsroots

SOCKET = '/run/agent-speech/speech.sock'
MAX_BYTES = 5 * 1024 * 1024


class AudioIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    audio: str | None = Field(default=None, min_length=1, max_length=MAX_BYTES * 4 // 3 + 8)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    format: Literal['mp3', 'webm', 'ogg', 'mp4', 'wav']
    language: Literal['auto', 'ru', 'en'] = 'auto'


def decode_audio(value):
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, 'The audio data is damaged') from None
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, 'Audio must be 5 MiB or smaller')
    return data


def read_attachment(value):
    path = Path(value)
    if path.suffix.lower() != '.mp3' or path.parent.name != 'attachments':
        raise HTTPException(400, 'Choose an MP3 among the attachments')
    try:
        parent = fsroots.resolve_dir(str(path.parent), config.PORTAL_ROOTS)
        fd = os.open(parent / path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HTTPException(403, 'File not allowed')
            data = stream.read(MAX_BYTES + 1)
    except fsroots.FsError as error:
        raise HTTPException(error.status, error.detail) from None
    except (OSError, ValueError):
        raise HTTPException(404, 'MP3 unavailable') from None
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, 'Audio must be 5 MiB or smaller')
    return data


async def recognize(data, format, language):
    transport = httpx.AsyncHTTPTransport(uds=SOCKET)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=180) as client:
            response = await client.post('http://speech/transcribe', content=data,
                                         headers={'Content-Type':'application/octet-stream', 'X-Audio-Format':format, 'X-Audio-Language':language})
        result = response.json()
    except httpx.TimeoutException:
        raise HTTPException(504, 'Transcription took too long. Try a shorter recording.') from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Speech recognition is temporarily unavailable') from None
    if response.status_code != 200:
        allowed = {400, 413, 422, 429, 503}
        status = response.status_code if response.status_code in allowed else 503
        detail = result.get('detail') if isinstance(result, dict) else None
        raise HTTPException(status, detail if isinstance(detail, str) else 'Could not transcribe the audio')
    if not isinstance(result, dict) or not isinstance(result.get('text'), str) or len(result['text']) > 60000:
        raise HTTPException(502, 'Invalid transcription response')
    return {'text':result['text'], 'language':result.get('language')}


def build_router():
    router = APIRouter()

    @router.get('/api/speech')
    async def available():
        return {'available':Path(SOCKET).is_socket(), 'max_seconds':180, 'max_bytes':MAX_BYTES}

    @router.post('/api/speech')
    async def transcribe(body: AudioIn, request: Request):
        if (body.audio is None) == (body.path is None) or body.path is not None and body.format != 'mp3':
            raise HTTPException(400, 'Send a recording or one MP3 attachment')
        data = await asyncio.to_thread(read_attachment, body.path) if body.path is not None else decode_audio(body.audio)
        result = await recognize(data, body.format, body.language)
        if not request.scope.get('portal_valid', lambda: True)():
            raise HTTPException(403, 'forbidden')
        return result

    return router

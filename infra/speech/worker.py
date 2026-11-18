import io
import json
import os
from pathlib import Path
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import av
import numpy as np
from faster_whisper import WhisperModel

MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 180
FORMATS = {'mp3':'mp3', 'webm':'matroska', 'ogg':'ogg', 'mp4':'mov', 'wav':'wav'}
LOCK = threading.Lock()
MODEL = None


class AudioError(Exception):
    pass


def decode(raw, format):
    chunks = []
    count = 0
    start = time.monotonic()
    try:
        with av.open(io.BytesIO(raw), format=FORMATS[format], options={'protocol_whitelist':'pipe'}) as container:
            stream = next((s for s in container.streams if s.type == 'audio'), None)
            if stream is None:
                raise AudioError('В файле нет звуковой дорожки')
            stream.thread_type = 'AUTO'
            resampler = av.AudioResampler(format='fltp', layout='mono', rate=16000)
            for frame in container.decode(stream):
                if time.monotonic() - start > 15:
                    raise AudioError('Слишком сложный аудиофайл')
                for converted in resampler.resample(frame):
                    values = converted.to_ndarray().reshape(-1)
                    count += len(values)
                    if count > 16000 * MAX_SECONDS:
                        raise AudioError('Запись должна быть не длиннее 3 минут')
                    chunks.append(values.copy())
            for converted in resampler.resample(None):
                values = converted.to_ndarray().reshape(-1)
                count += len(values)
                if count > 16000 * MAX_SECONDS:
                    raise AudioError('Запись должна быть не длиннее 3 минут')
                chunks.append(values.copy())
    except AudioError:
        raise
    except Exception:
        raise AudioError('Не удалось прочитать аудио. Проверьте формат файла.') from None
    if not chunks:
        raise AudioError('Аудиозапись пуста')
    return np.concatenate(chunks)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def result(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.result(200, {'available':True}) if self.path == '/health' else self.result(404, {'detail':'Not found'})

    def do_POST(self):
        global MODEL
        if self.path != '/transcribe':
            return self.result(404, {'detail':'Not found'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        format = self.headers.get('X-Audio-Format')
        language = self.headers.get('X-Audio-Language', 'auto')
        if size <= 0 or size > MAX_BYTES or format not in FORMATS or language not in ('auto', 'ru', 'en'):
            return self.result(400, {'detail':'Недопустимый размер или формат аудио'})
        if not LOCK.acquire(blocking=False):
            return self.result(429, {'detail':'Распознаётся другая запись. Попробуйте немного позже.'})
        try:
            self.connection.settimeout(20)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise AudioError('Аудиозапись получена не полностью')
            audio = decode(raw, format)
            if MODEL is None:
                MODEL = WhisperModel('/opt/agent-speech/model', device='cpu', compute_type='int8', cpu_threads=2, num_workers=1, local_files_only=True)
            segments, info = MODEL.transcribe(audio, language=None if language == 'auto' else language,
                                              beam_size=3, best_of=1, temperature=0, vad_filter=True, condition_on_previous_text=False)
            text = ' '.join(segment.text.strip() for segment in segments).strip()
            if len(text) > 60000:
                raise AudioError('Результат слишком большой. Разделите запись на части.')
            self.result(200, {'text':text, 'language':info.language})
        except AudioError as error:
            self.result(422, {'detail':str(error)})
        except Exception:
            self.result(503, {'detail':'Не удалось распознать запись. Попробуйте ещё раз.'})
        finally:
            LOCK.release()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == '__main__':
    socket = Path('/run/agent-speech/speech.sock')
    if socket.is_socket():
        socket.unlink()
    with Server(str(socket), Handler) as server:
        os.chmod(socket, 0o660)
        server.serve_forever()

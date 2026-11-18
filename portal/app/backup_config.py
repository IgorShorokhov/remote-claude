import argparse
import hmac
import os
import sys

from fastapi import HTTPException
from .backup_agent import DEFAULT_PROVIDER_URL, available_models, key_path, provider_config, provider_path, transport_key, write_private, write_provider


def install(stream, replace=False, base_url=None):
    key = stream.read(4098).strip()
    if not key or len(key) > 4096 or not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Недопустимый формат ключа')
    target = key_path()
    existed = target.exists()
    if replace:
        write_private(target, key)
    else:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if not hmac.compare_digest(transport_key(), key):
                raise ValueError('Другой ключ уже установлен; замена требует подтверждения replace') from None
        else:
            with os.fdopen(fd, 'w') as dest:
                os.fchmod(dest.fileno(), 0o600)
                dest.write(key)
                dest.flush()
                os.fsync(dest.fileno())
    if not hmac.compare_digest(transport_key(), key):
        raise ValueError('Повторная проверка ключа не пройдена')
    if base_url is not None:
        if existed and not replace and base_url != provider_config()['base_url']:
            raise ValueError('Смена провайдера требует подтверждения replace')
        write_private(provider_path(), write_provider(base_url))


def main():
    parser = argparse.ArgumentParser(description='Только конфигурация резервного API-агента: install из stdin; check только GET /models')
    parser.add_argument('mode', choices=['install', 'check'])
    args = parser.parse_args()
    try:
        if args.mode == 'install':
            if sys.stdin.isatty():
                raise ValueError('Ключ принимается только защищённым потоком stdin')
            install(sys.stdin)
            print('Ключ установлен: 0600, повторное чтение совпало; HTTP ещё не проверен')
        else:
            models = available_models()
            print(f'Provider HTTP 200; доступно моделей: {len(models)}; генераций: 0')
    except (HTTPException, OSError, ValueError) as error:
        detail = error.detail if isinstance(error, HTTPException) else 'Установка/проверка отказана; конфигурация не подтверждена'
        print(detail, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

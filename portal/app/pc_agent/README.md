# Агент для компьютера

Через него Claude из чатов портала запускает команды и правит файлы на компьютере владельца аккаунта (Windows, Linux, macOS). Решение 217.

```
Claude (чат) ──MCP stdio──► pc_mcp.py ──unix-сокет──► портал (browser_bridge.py)
                                                          │ WebSocket /ext/bridge, токен вида pc
                                                          ▼
                                            portal_agent.py на компьютере (исходящее соединение)
```

- `portal_agent.py`: один файл, стандартная библиотека, Python 3.9+. Команды `setup`, `run`, `pause`, `resume`, `status`, `uninstall`.
- `setup_windows.cmd`, `setup_unix.sh`: шаблоны установщиков; портал подставляет адрес, агента (base64) и закреплённую сборку Python (`python-embed.json`: версия, адреса и SHA-256 для amd64 и arm64). Скачивание: `GET /api/devices/download/windows|unix|agent`.
- Windows: ставится для пользователя в `%LOCALAPPDATA%\ClaudePortal` без прав администратора, свой Python embeddable, автозапуск в `HKCU\...\Run`, ярлыки в меню «Пуск» (Pause, Resume, Status, Remove). Linux: unit systemd пользователя или XDG autostart; macOS: LaunchAgent.
- Журнал `activity.log` рядом с настройками: время и команда или путь, содержимое файлов не пишется. Пауза: файл `paused`.
- Обновление агента: поднять `VERSION` в `portal_agent.py` и выложить портал. Компьютер со старой версией при подключении получает новый файл с SHA-256, проверяет его, сохраняет старый как `portal_agent.py.bak` и перезапускается (решение 226). В окне настроек это можно выключить. Агенты до 0.3.2 обновляются вручную.
- Обновить закреплённый Python: скачать новый embeddable, посчитать SHA-256, поправить `python-embed.json`.
- Проверка: `portal/tests/test_pc_agent.py` (настоящий сервер и процессы агента под Linux). Ветки Windows проверены только по построению команд.

# Remote Claude

Свой сайт с Claude Code на вашем VPS: чаты с Claude Code в браузере с компьютера и телефона, файлы, промпты, скиллы, инструкции и память. Входят по аккаунту Google; владелец решает, кто ещё может входить и какие разделы видит.

Claude работает от вашей подписки Claude Pro или Max (вход один раз на сервере).

## Что нужно

- VPS с Ubuntu 24.04 x86_64: от 2 ГБ памяти (лучше 4 ГБ), от 15 ГБ диска, доступ по SSH с `sudo`.
- Подписка Claude Pro или Max.
- Аккаунт Google (для входа на сайт) и проект в Google Cloud (бесплатно, создаётся по инструкции).
- Свой домен с A-записью на IP сервера **или** бесплатный аккаунт Tailscale.

## Установка

Установку может вести ваш ИИ-агент: дайте ему файл [AGENT.md](AGENT.md), там всё по шагам. Коротко:

```
git clone <этот репозиторий> remote-claude && cd remote-claude
./install.sh configure          # вопросы: почта, домен, разделы
sudo ./install.sh               # установка, 10-25 минут
sudo ./install.sh google        # вы сами: Client ID и secret Google
sudo ./install.sh claude-login  # вы сами: вход в Claude
sudo ./install.sh doctor        # проверка
```

## Разделы

| Раздел | Что даёт | Цена |
|---|---|---|
| Code Web | чаты с Claude Code, файлы, история (есть всегда) | лимит подписки |
| Console | терминал сервера в браузере, только владелец | — |
| Prompts | сохранённые промпты | — |
| Skills | общие скиллы Claude с редактором | — |
| System instructions | свои инструкции для Claude у каждого аккаунта | — |
| Memory | Claude помнит факты о пользователе; ночью обновляется | немного лимита |
| Access | владелец добавляет почты и открывает или закрывает разделы каждому | — |
| Computers | подключить свой компьютер и Chrome, чтобы Claude ими управлял | — |
| API Agent | агент на моделях OpenAI-совместимого API, только владелец | оплата API |
| Голосовой ввод | распознавание речи на сервере | +1 ГБ памяти |

Наборы: **минимум** (Code Web, Prompts, Skills, Instructions), **обычный** (+ Console, Memory, Access), **всё**. Поменять: `kit.conf`, затем `sudo ./install.sh`.

## Как это устроено

- Claude Code, портал и чаты работают от пользователя `claude` без пароля и без `sudo`.
- Наружу смотрит только HTTPS: свой домен (Caddy с сертификатом Let's Encrypt) или Tailscale Funnel. Портал, вход и веб-сервер слушают только внутри сервера.
- Вход: Google через oauth2-proxy. Пускают только почты из списка, администратор один — владелец.
- Если сервер в стране, где Claude недоступен, запросы к Anthropic идут через Cloudflare WARP (`PROXY="warp"`).
- Ночные копии (если включены): `/var/backups/remote-claude`, 7 дней.

## Обновление

```
cd remote-claude && git pull && sudo ./install.sh
```

## Если что-то сломалось

`sudo ./install.sh doctor` пишет, что не так и что сделать. Журнал установки: `/var/log/remote-claude-install.log`. Службы: `agent-portal`, `agent-host`, `agent-tmux`, `caddy`, `oauth2-proxy`, `agent-front` (домен).

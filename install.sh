#!/usr/bin/env bash
# Remote Claude: установка на чистый Ubuntu 24.04. Все шаги можно повторять: уже сделанное не ломается и не дублируется.
#   ./install.sh configure          вопросы и запись kit.conf
#   sudo ./install.sh check-config  проверить kit.conf
#   sudo ./install.sh               установить или обновить всё
#   sudo ./install.sh step ИМЯ      один шаг: base user network claude portal signin web extras
#   sudo ./install.sh doctor        проверить, что всё работает, и сказать, что осталось сделать человеку
#   sudo ./install.sh google        (человек) ввести Client ID и Client secret Google
#   sudo ./install.sh claude-login  (человек) войти в Claude Code своей подпиской
set -euo pipefail
cd "$(dirname "$0")"
KIT=$(pwd)
LOG=/var/log/remote-claude-install.log
STEPS=(base user network claude portal signin web extras)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() {
  printf '\n\033[31mШАГ НЕ ПРОШЁЛ: %s\033[0m\n' "$*" >&2
  echo "Ничего не исправляйте наугад. Покажите человеку последние 30 строк выше и файл $LOG." >&2
  exit 1
}

need_root() { [ "$(id -u)" = 0 ] || { echo "запускайте через sudo: sudo ./install.sh ${*:-}" >&2; exit 1; }; }

load_config() {
  local env
  env=$(python3 -I -B kit/configure.py --env) || die "kit.conf с ошибкой (см. строку ОШИБКА выше)"
  eval "$env"
  export KIT_OWNER_EMAIL KIT_PORTAL_FEATURES KIT_TITLE
  HOST=""
  if [ "$KIT_SITE_MODE" = domain ]; then HOST=$KIT_DOMAIN
  elif [ -s /etc/agent-system/kit-host ]; then HOST=$(cat /etc/agent-system/kit-host)
  fi
}

host_env() {
  [ -n "$HOST" ] || die "адрес сайта ещё неизвестен: сначала шаг network"
  export KIT_PORTAL_HOST=$HOST KIT_OWNER_EMAIL KIT_FEATURES=$KIT_PORTAL_FEATURES KIT_PORTAL_ROOTS=/home/claude/work KIT_BANNER=$KIT_TITLE KIT_NO_GUARD=1
}

preflight() {
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || die "нужна Ubuntu 24.04, а здесь ${PRETTY_NAME:-неизвестная система}"
  [ "$(uname -m)" = x86_64 ] || die "нужен процессор x86_64 (amd64), а здесь $(uname -m)"
  local mem disk
  mem=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  disk=$(df -Pm / | awk 'NR==2 {print $4}')
  [ "$mem" -ge 1800 ] || die "памяти ${mem} МБ, нужно хотя бы 2 ГБ (лучше 4 ГБ)"
  [ "$disk" -ge 8000 ] || die "свободно ${disk} МБ на диске, нужно хотя бы 8 ГБ"
  if [ "${KIT_FEATURE_VOICE:-no}" = yes ] && [ "$mem" -lt 3500 ]; then
    echo "внимание: голосовой ввод на ${mem} МБ памяти может работать медленно (лучше 4 ГБ)"
  fi
  curl -fsS -m 15 -o /dev/null https://github.com || die "нет интернета (github.com не открывается)"
}

step_base() {
  say "Шаг 1/8: обновления, пакеты, файл подкачки"
  bash infra/20-base.sh || die "base"
  local p missing=""
  for p in git python3 python3-venv tmux jq; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  [ -z "$missing" ] || DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -qq $missing >/dev/null || die "пакеты:$missing"
}

step_user() {
  say "Шаг 2/8: пользователь claude"
  bash kit/user.sh || die "user"
}

step_network() {
  say "Шаг 3/8: сеть (прокси к Anthropic и адрес сайта)"
  if [ "$KIT_PROXY" = warp ]; then
    KIT_NO_GUARD=1 bash infra/29-warp.sh || die "Cloudflare WARP"
    bash infra/40-http-proxy.sh apply || die "прокси через WARP"
  else
    install -d -m 755 /etc/agent-system
    if grep -q '^HTTPS_PROXY=' /etc/agent-system/proxy.env 2>/dev/null; then
      echo "раньше был WARP, теперь PROXY=none: убираю прокси (сам WARP не удаляю)"
    fi
    printf 'NO_PROXY=localhost,127.0.0.1,::1\nno_proxy=localhost,127.0.0.1,::1\n' > /etc/agent-system/proxy.env
    chmod 644 /etc/agent-system/proxy.env
    local code
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' https://claude.ai/ || true)
    case "$code" in
      000) die "claude.ai не открывается с сервера. Если сервер в России, поставьте PROXY=\"warp\" в kit.conf" ;;
      403) die "claude.ai отвечает 403: похоже, Anthropic не работает в стране сервера. Поставьте PROXY=\"warp\" в kit.conf" ;;
      *) echo "claude.ai открывается напрямую (ответ $code)" ;;
    esac
  fi
  if [ "$KIT_SITE_MODE" = tailscale ]; then
    bash kit/funnel.sh up || die "Tailscale"
    HOST=$(cat /etc/agent-system/kit-host)
  else
    bash kit/front.sh dns "$KIT_DOMAIN" || die "домен $KIT_DOMAIN не указывает на этот сервер"
  fi
  echo "адрес сайта: https://$HOST"
}

step_claude() {
  say "Шаг 4/8: Claude Code, tmux и служба чатов"
  bash infra/42-claude-code.sh || die "Claude Code"
  bash infra/43-tmux.sh || die "tmux"
  bash infra/96-agent-host.sh apply || die "служба чатов agent-host"
}

step_portal() {
  say "Шаг 5/8: портал"
  host_env
  bash infra/54-portal-base.sh apply || die "основа портала"
  find portal -name __pycache__ -prune -exec rm -rf {} +
  tar -C portal -czf - app static requirements.txt | /usr/local/sbin/agent-portal-install || die "установка кода портала"
}

step_signin() {
  say "Шаг 6/8: вход через Google (oauth2-proxy)"
  host_env
  bash infra/51-oauth2-proxy.sh || die "oauth2-proxy"
}

step_web() {
  say "Шаг 7/8: веб-сервер Caddy, почты и адрес в интернете"
  host_env
  bash infra/50-caddy.sh apply || die "Caddy"
  python3 -I -B kit/roles.py | /usr/local/sbin/agent-apply-roles || die "список почт"
  if [ "$KIT_SITE_MODE" = domain ]; then
    bash kit/front.sh apply "$KIT_DOMAIN" "$KIT_OWNER_EMAIL" || die "сайт https://$KIT_DOMAIN"
  else
    bash kit/funnel.sh publish || die "Tailscale Funnel"
  fi
}

step_extras() {
  say "Шаг 8/8: дополнительные разделы"
  if [ "$KIT_FEATURE_ACCESS" = yes ]; then
    bash infra/59-access-bridge.sh install || die "мост страницы Access"
  fi
  if [ "$KIT_FEATURE_VOICE" = yes ]; then
    echo "голосовой ввод: качаю модель распознавания (около 500 МБ), это несколько минут"
    python3 -I infra/89-speech-setup.py deploy infra >/dev/null || die "голосовой ввод"
    echo "голосовой ввод работает"
  fi
  if [ "$KIT_BACKUPS" = yes ]; then bash kit/backup.sh install || die "резервные копии"; else bash kit/backup.sh remove; fi
}

run_steps() {
  local s
  for s in "$@"; do "step_$s"; done
}

MODE=${1:-all}
case "$MODE" in
  configure) python3 -I -B kit/configure.py ;;
  check-config) python3 -I -B kit/configure.py --check ;;
  doctor) need_root doctor; load_config; bash kit/doctor.sh ;;
  google) need_root google; [ -t 0 ] || die "нужен терминал: запускает человек в своём SSH"; exec /usr/local/sbin/agent-oauth-secret ;;
  claude-login)
    need_root claude-login
    [ -t 0 ] || die "нужен терминал: запускает человек в своём SSH"
    echo "Откроется Claude Code. Выберите тему, затем вход подпиской Claude (Pro или Max): откройте ссылку, войдите, вставьте код."
    echo "Когда увидите приглашение ввода, напишите /exit."
    exec runuser -l claude -c 'set -a; . /etc/agent-system/proxy.env; set +a; cd ~/work && exec ~/.local/bin/claude' ;;
  all|step)
    need_root "$@"
    exec > >(tee -a "$LOG") 2>&1
    echo "---- $(date -Is) install.sh $*"
    load_config
    python3 -I -B kit/configure.py --check
    preflight
    if [ "$MODE" = all ]; then run_steps "${STEPS[@]}"
    else
      [ -n "${2:-}" ] && [[ " ${STEPS[*]} " == *" $2 "* ]] || die "шаги: ${STEPS[*]}"
      run_steps "$2"
    fi
    say "Проверка"
    bash kit/doctor.sh || true
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac

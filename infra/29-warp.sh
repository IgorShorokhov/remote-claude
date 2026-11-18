#!/usr/bin/env bash
set -euo pipefail
note() { echo "[29-warp] $*"; }
die() { echo "[29-warp] ОШИБКА: $*"; exit 1; }
[ "$(id -u)" -eq 0 ] || die "нужен root (sudo bash -s)"
PHASE="${1:-all}"
PORT=40000
KEYRING=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
LIST=/etc/apt/sources.list.d/cloudflare-client.list

[ -n "${KIT_NO_GUARD:-}" ] || systemctl is-active --quiet agent-rollback.timer || die "таймер автоотката не взведен (27-guard.sh arm)"

if ! command -v warp-cli >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq curl gpg ca-certificates lsb-release >/dev/null
  if [ ! -s "$KEYRING" ]; then
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o "$KEYRING"
    note "ключ репозитория добавлен"
  fi
  echo "deb [arch=amd64 signed-by=$KEYRING] https://pkg.cloudflareclient.com/ noble main" > "$LIST"
  apt-get update -qq
  apt-get install -y -qq cloudflare-warp >/dev/null
  note "cloudflare-warp установлен"
else
  note "cloudflare-warp уже установлен"
fi

systemctl enable --now warp-svc >/dev/null 2>&1
for _ in $(seq 1 20); do
  warp-cli --accept-tos status >/dev/null 2>&1 && break
  sleep 1
done

if [ "$PHASE" = "help" ]; then
  warp-cli --version
  warp-cli --help
  exit 0
fi

W() { warp-cli --accept-tos "$@"; }
HELP=$(warp-cli --help 2>&1)
has() { grep -qE "^\s+$1\b" <<<"$HELP"; }

if W status 2>&1 | grep -qi "registration missing"; then
  if has registration; then W registration new
  elif has register; then W register
  else die "нет команды регистрации в warp-cli --help"; fi
  note "зарегистрирован"
else
  note "регистрация уже есть"
fi

if has mode; then W mode proxy
elif has set-mode; then W set-mode proxy
else die "нет команды выбора режима"; fi
if has proxy; then W proxy port "$PORT"
elif has set-proxy-port; then W set-proxy-port "$PORT"
else die "нет команды порта прокси"; fi
note "режим proxy, порт $PORT"

SET=$(W settings list 2>&1 || true)
grep -i "mode" <<<"$SET" | head -3 || true
grep -qiE "mode.*proxy" <<<"$SET" || die "режим proxy не подтвержден, connect отменен"

if W status 2>&1 | grep -qi "^Status update: Connected"; then
  note "уже подключен"
else
  W connect
  for _ in $(seq 1 60); do
    W status 2>&1 | grep -qi "Connected" && break
    sleep 1
  done
fi
W status 2>&1 | head -3

note "слушающие порты $PORT:"
LISTEN=$(ss -ltnH "sport = :$PORT" | awk '{print $4}')
[ -n "$LISTEN" ] || die "никто не слушает порт $PORT"
printf '%s\n' "$LISTEN"
while IFS= read -r addr; do
  case "$addr" in
    127.0.0.1:$PORT|"[::1]:$PORT") ;;
    *) die "прокси слушает не на loopback: $addr" ;;
  esac
done <<<"$LISTEN"
T_PROXY=$(curl -s -m 20 --proxy socks5h://127.0.0.1:$PORT https://www.cloudflare.com/cdn-cgi/trace | grep '^warp=' || true)
T_DIRECT=$(curl -s -m 20 https://www.cloudflare.com/cdn-cgi/trace | grep '^warp=' || true)
note "через прокси: ${T_PROXY:-пусто}; напрямую: ${T_DIRECT:-пусто}"
[ "$T_PROXY" = "warp=on" ] || die "через прокси не warp=on"
[ "$T_DIRECT" = "warp=off" ] || die "напрямую не warp=off"
systemctl is-enabled --quiet warp-svc && note "warp-svc в автозапуске"
note "готово"

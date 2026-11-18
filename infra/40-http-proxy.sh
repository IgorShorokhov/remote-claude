#!/usr/bin/env bash
set -euo pipefail
MODE=${1:-apply}
note() { echo "[40-http-proxy] $*"; }
die() { echo "[40-http-proxy] ОШИБКА: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "нужен root (sudo bash -s)"
case "$MODE" in apply|check) ;; *) die "режим: apply|check" ;; esac

WARP_PORT=40000
BRIDGE_PORT=8118
ENVDIR=/etc/agent-system
ENVFILE=$ENVDIR/proxy.env
PCONF=/etc/privoxy/config
DROPIN=/etc/systemd/system/privoxy.service.d/agent.conf
TRACE=https://www.cloudflare.com/cdn-cgi/trace
CHANGED=0

trace_via() { curl -s -m 25 --proxy "$1" "$2" 2>/dev/null | grep -E '^warp=' || true; }

only_loopback() {
  local port=$1 bad
  bad=$(ss -ltnH "sport = :$port" | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' || true)
  [ -z "$bad" ] || die "порт $port слушает не только loopback: $bad"
  ss -ltnH "sport = :$port" | grep -q . || return 1
}

warp-cli --accept-tos status 2>/dev/null | grep -q 'Connected' || die "WARP не подключён (29-warp.sh); ничего не меняю"
only_loopback "$WARP_PORT" || die "WARP не слушает 127.0.0.1:$WARP_PORT"

if [ "$(trace_via "http://127.0.0.1:$WARP_PORT" "$TRACE")" = "warp=on" ]; then
  PROXY="http://127.0.0.1:$WARP_PORT"
  note "WARP принимает HTTP-прокси сам, мост не нужен"
elif [ "$MODE" = check ]; then
  PROXY="http://127.0.0.1:$BRIDGE_PORT"
else
  note "WARP HTTP не принимает, нужен мост privoxy"
  if ! dpkg -s privoxy >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends privoxy >/dev/null
    CHANGED=1
    note "privoxy установлен"
  fi
  [ -f "$PCONF.orig" ] || cp -p "$PCONF" "$PCONF.orig"
  tmp=$(mktemp)
  cat > "$tmp" <<CONF
user-manual /usr/share/doc/privoxy/user-manual
confdir /etc/privoxy
logdir /var/log/privoxy
logfile logfile
listen-address 127.0.0.1:$BRIDGE_PORT
toggle 0
enable-remote-toggle 0
enable-remote-http-toggle 0
enable-edit-actions 0
accept-intercepted-requests 0
forward-socks5t / 127.0.0.1:$WARP_PORT .
CONF
  RESTART=0
  if ! cmp -s "$tmp" "$PCONF"; then install -m 644 -o root -g root "$tmp" "$PCONF"; CHANGED=1; RESTART=1; note "конфиг privoxy обновлён"; fi
  rm -f "$tmp"
  mkdir -p "$(dirname "$DROPIN")"
  tmp=$(mktemp)
  printf '[Unit]\nAfter=warp-svc.service\nWants=warp-svc.service\n' > "$tmp"
  if ! cmp -s "$tmp" "$DROPIN" 2>/dev/null; then install -m 644 "$tmp" "$DROPIN"; systemctl daemon-reload; CHANGED=1; RESTART=1; note "drop-in обновлён"; fi
  rm -f "$tmp"
  systemctl is-enabled --quiet privoxy || { systemctl enable privoxy >/dev/null 2>&1; CHANGED=1; }
  if [ "$RESTART" = 1 ] || ! systemctl is-active --quiet privoxy; then systemctl restart privoxy; sleep 2; fi
  PROXY="http://127.0.0.1:$BRIDGE_PORT"
fi

if [ "$MODE" = apply ]; then
  mkdir -p "$ENVDIR"
  chmod 755 "$ENVDIR"
  tmp=$(mktemp)
  cat > "$tmp" <<ENV
HTTPS_PROXY=$PROXY
HTTP_PROXY=$PROXY
https_proxy=$PROXY
http_proxy=$PROXY
NO_PROXY=localhost,127.0.0.1,::1
no_proxy=localhost,127.0.0.1,::1
ENV
  if ! cmp -s "$tmp" "$ENVFILE" 2>/dev/null; then install -m 644 -o root -g root "$tmp" "$ENVFILE"; CHANGED=1; note "записан $ENVFILE"; fi
  rm -f "$tmp"
fi

[ -f "$ENVFILE" ] || die "нет $ENVFILE (сначала apply)"
P=$(grep '^HTTPS_PROXY=' "$ENVFILE" | cut -d= -f2-)
case "$P" in http://127.0.0.1:*) ;; *) die "в $ENVFILE неожиданный адрес прокси" ;; esac
[ "$(stat -c '%a %U' "$ENVFILE")" = "644 root" ] || die "права $ENVFILE не 644 root"
if grep -qiE 'socks|ALL_PROXY' "$ENVFILE"; then die "в $ENVFILE есть socks или ALL_PROXY"; fi

only_loopback "$WARP_PORT" || die "WARP не слушает $WARP_PORT"
if [ "$P" = "http://127.0.0.1:$BRIDGE_PORT" ]; then only_loopback "$BRIDGE_PORT" || die "мост не слушает $BRIDGE_PORT"; fi

r=$(trace_via "$P" "$TRACE")
note "cloudflare.com через $P -> ${r:-нет ответа}"
[ "$r" = "warp=on" ] || die "через прокси нет warp=on"

if [ "$MODE" = check ]; then
  r=$(trace_via "$P" https://claude.ai/cdn-cgi/trace)
  note "claude.ai через $P -> ${r:-нет ответа}"
  [ "$r" = "warp=on" ] || die "claude.ai через прокси: нет warp=on"
  note "проверка пройдена"
  exit 0
fi

if [ "$CHANGED" = 0 ]; then note "изменений нет"; else note "готово, прокси: $P"; fi

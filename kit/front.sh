#!/usr/bin/env bash
# Свой домен: второй Caddy (agent-front) держит порты 80 и 443, сам получает сертификат Let's Encrypt и передаёт всё
# основному Caddy на 127.0.0.1:8080, где проверяются вход и роли. Основной Caddy при этом остаётся закрытым от интернета.
#   bash kit/front.sh dns ДОМЕН           проверить, что A-запись домена указывает на этот сервер
#   bash kit/front.sh apply ДОМЕН ПОЧТА   поставить или обновить agent-front
#   bash kit/front.sh render ДОМЕН ПОЧТА  напечатать конфигурацию (для проверки)
set -euo pipefail
MODE=${1:-}
DOMAIN=${2:-}
EMAIL=${3:-}
CONF=/etc/caddy/front.Caddyfile
UNIT=/etc/systemd/system/agent-front.service
DATA=/var/lib/agent-front

[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "неверное имя домена: $DOMAIN" >&2; exit 2; }

render() {
  [[ "$EMAIL" =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]] || { echo "неверная почта для Let's Encrypt" >&2; exit 2; }
  cat <<EOT
{
	admin off
	email $EMAIL
	storage file_system $DATA
}

$DOMAIN {
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
EOT
}

unit() {
  cat <<'EOT'
[Unit]
Description=Remote Claude: HTTPS for the own domain (Let's Encrypt), passes everything to the main Caddy on 127.0.0.1:8080
After=network-online.target caddy.service
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/front.Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
StateDirectory=agent-front
StateDirectoryMode=0700
Environment=HOME=/var/lib/agent-front XDG_DATA_HOME=/var/lib/agent-front XDG_CONFIG_HOME=/var/lib/agent-front
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
MemoryMax=256M

[Install]
WantedBy=multi-user.target
EOT
}

dns() {
  local mine want
  want=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}')
  [ -n "$want" ] || { echo "у домена $DOMAIN нет A-записи. Добавьте у регистратора запись A: $DOMAIN -> IP сервера и подождите 5-30 минут" >&2; return 1; }
  mine=$(curl -4 -fsS -m 15 https://api.ipify.org || curl -4 -fsS -m 15 https://ifconfig.me || true)
  if [ -z "$mine" ]; then
    echo "не удалось узнать внешний IP сервера; домен указывает на $want, проверьте сами"
    return 0
  fi
  [ "$want" = "$mine" ] || { echo "домен $DOMAIN указывает на $want, а IP этого сервера $mine. Исправьте A-запись и подождите" >&2; return 1; }
  echo "домен $DOMAIN указывает на этот сервер ($mine)"
}

apply() {
  [ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }
  command -v caddy >/dev/null || { echo "нет caddy (сначала шаг web)" >&2; exit 1; }
  local tmp changed=0
  tmp=$(mktemp)
  render > "$tmp"
  caddy validate --config "$tmp" --adapter caddyfile >/dev/null 2>"$tmp.err" || { tail -n 5 "$tmp.err" >&2; rm -f "$tmp" "$tmp.err"; exit 1; }
  if ! cmp -s "$tmp" "$CONF"; then install -m 644 -o root -g root "$tmp" "$CONF"; changed=1; fi
  unit > "$tmp"
  if ! cmp -s "$tmp" "$UNIT"; then install -m 644 -o root -g root "$tmp" "$UNIT"; systemctl daemon-reload; changed=1; fi
  rm -f "$tmp" "$tmp.err"
  ss -ltnpH 2>/dev/null | awk '$4 ~ /:(80|443)$/' | grep -v '"caddy"' && { echo "порт 80 или 443 занят другой программой (выше); её нужно остановить" >&2; exit 1; }
  systemctl enable agent-front >/dev/null 2>&1
  if [ "$changed" = 1 ] || ! systemctl is-active --quiet agent-front; then systemctl restart agent-front; fi
  if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
    echo "ufw включён: открыты 80 и 443"
  fi
  local i code
  for i in $(seq 1 36); do
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$DOMAIN/" || true)
    case "$code" in 302|401|200) echo "сайт https://$DOMAIN отвечает (код $code), сертификат получен"; return 0 ;; esac
    sleep 5
  done
  echo "https://$DOMAIN не ответил за 3 минуты (последний код $code). Журнал: journalctl -u agent-front -n 30" >&2
  echo "Частые причины: порты 80/443 закрыты у хостинга (панель провайдера, firewall), A-запись ещё не обновилась." >&2
  return 1
}

case "$MODE" in
  dns) dns ;;
  apply) apply ;;
  render) render ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac

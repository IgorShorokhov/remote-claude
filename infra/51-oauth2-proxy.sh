#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
changed=0
restart=0
LASTPUT=0

VER=v7.15.5
URL=https://github.com/oauth2-proxy/oauth2-proxy/releases/download/$VER/oauth2-proxy-$VER.linux-amd64.tar.gz
SHA=f63f94bf72c5f46ab002a0a275aa8b3cf19b4d828aed08a13978cb9a62c3a1fd
BIN=/usr/local/bin/oauth2-proxy
SEC=/etc/agent-system/secrets
ENVF=$SEC/oauth2-proxy.env
ALLOWED=/etc/agent-system/allowed-emails.txt

[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }

put() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  LASTPUT=0
  if [ ! -f "$dest" ] || ! cmp -s "$tmp" "$dest" || [ "$(stat -c %a "$dest")" != "$mode" ] || [ "$(stat -c %U:%G "$dest")" != "$owner" ]; then
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    changed=1
    LASTPUT=1
  fi
  rm -f "$tmp"
}

if [ -n "${KIT_PORTAL_HOST:-}" ]; then
  # Servers installed from the kit (kit/) name their address in kit.conf instead of Tailscale.
  [[ "$KIT_PORTAL_HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "KIT_PORTAL_HOST: неверное имя" >&2; exit 1; }
  HOST=$KIT_PORTAL_HOST
else
HOST=$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
case "$HOST" in
  *.ts.net) ;;
  *) echo "нет имени *.ts.net у этого узла (MagicDNS выключен?)" >&2; exit 1 ;;
esac
fi

if [ "$("$BIN" --version 2>/dev/null | awk '{print $2}' || true)" != "$VER" ]; then
  d=$(mktemp -d)
  curl -fsSL "$URL" -o "$d/o.tgz"
  if ! echo "$SHA  $d/o.tgz" | sha256sum -c --quiet -; then
    echo "контрольная сумма не совпала, ничего не ставлю" >&2
    rm -rf "$d"
    exit 1
  fi
  mkdir "$d/x"
  tar -xzf "$d/o.tgz" -C "$d/x" "oauth2-proxy-$VER.linux-amd64/oauth2-proxy"
  install -m 755 -o root -g root "$d/x/oauth2-proxy-$VER.linux-amd64/oauth2-proxy" "$BIN"
  rm -rf "$d"
  changed=1
  restart=1
fi

if ! getent passwd oauth2-proxy >/dev/null; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin oauth2-proxy
  changed=1
fi

install -d -m 755 /etc/agent-system
if [ "$(stat -c %a:%U $SEC 2>/dev/null || true)" != "700:root" ]; then
  install -d -m 700 -o root -g root $SEC
  changed=1
fi

if [ ! -e "$ALLOWED" ]; then
  put "$ALLOWED" 644 root:root < /dev/null
fi

put /etc/oauth2-proxy/oauth2-proxy.cfg 644 root:root <<EOT
provider = "google"
scope = "openid email profile"
prompt = "select_account"
code_challenge_method = "S256"
http_address = "127.0.0.1:4180"
reverse_proxy = true
trusted_proxy_ips = ["127.0.0.1/32", "::1/128"]
real_client_ip_header = "X-Real-IP"
redirect_url = "https://$HOST/oauth2/callback"
upstreams = ["static://202"]
set_xauthrequest = true
pass_user_headers = false
pass_basic_auth = false
pass_access_token = false
authenticated_emails_file = "$ALLOWED"
cookie_name = "__Host-oauth2"
cookie_secure = true
cookie_httponly = true
cookie_samesite = "lax"
cookie_expire = "168h"
cookie_refresh = "0s"
session_cookie_minimal = true
skip_provider_button = true
cookie_csrf_per_request = true
banner = "${KIT_BANNER:-Claude-станция}"
standard_logging = true
auth_logging = true
request_logging = false
silence_ping_logging = true
EOT
[ "$LASTPUT" = 1 ] && restart=1

put /etc/systemd/system/oauth2-proxy.service 644 root:root <<'EOT'
[Unit]
Description=oauth2-proxy (вход через Google для портала)
After=network-online.target
Wants=network-online.target

[Service]
User=oauth2-proxy
Group=oauth2-proxy
EnvironmentFile=/etc/agent-system/secrets/oauth2-proxy.env
ExecStart=/usr/local/bin/oauth2-proxy --config=/etc/oauth2-proxy/oauth2-proxy.cfg
Restart=on-failure
RestartSec=5
MemoryMax=128M
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
EOT
[ "$LASTPUT" = 1 ] && { systemctl daemon-reload; restart=1; }

put /usr/local/sbin/agent-oauth-secret 750 root:root <<'EOT'
#!/usr/bin/env bash
set -euo pipefail
ENVF=/etc/agent-system/secrets/oauth2-proxy.env
[ "$(id -u)" = 0 ] || { echo "запускать так: sudo agent-oauth-secret" >&2; exit 1; }
[ -t 0 ] || { echo "нужен терминал (ssh -t)" >&2; exit 1; }
read -r -p "Client ID: " cid
read -r -s -p "Client secret (на экране не видно): " csec
echo
case "$cid" in
  *.apps.googleusercontent.com) ;;
  *) echo "Client ID должен кончаться на .apps.googleusercontent.com, ничего не сохранено" >&2; exit 1 ;;
esac
if ! [[ "$cid" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "в Client ID лишние символы, ничего не сохранено" >&2
  exit 1
fi
if ! [[ "$csec" =~ ^[A-Za-z0-9._~+/=-]{8,}$ ]]; then
  echo "Client secret пустой, короткий или с лишними символами, ничего не сохранено" >&2
  exit 1
fi
cookie=""
if [ -f "$ENVF" ]; then
  cookie=$(grep '^OAUTH2_PROXY_COOKIE_SECRET=' "$ENVF" | head -n1 | cut -d= -f2- || true)
fi
if [ -z "$cookie" ]; then
  cookie=$(openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_')
fi
umask 077
tmp=$(mktemp -p /etc/agent-system/secrets)
{
  printf 'OAUTH2_PROXY_CLIENT_ID=%s\n' "$cid"
  printf 'OAUTH2_PROXY_CLIENT_SECRET=%s\n' "$csec"
  printf 'OAUTH2_PROXY_COOKIE_SECRET=%s\n' "$cookie"
} > "$tmp"
chown root:root "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENVF"
systemctl enable oauth2-proxy >/dev/null 2>&1
systemctl restart oauth2-proxy
sleep 3
if systemctl is-active --quiet oauth2-proxy; then
  echo "сохранено, oauth2-proxy перезапущен"
else
  echo "сохранено, но oauth2-proxy не запустился: journalctl -u oauth2-proxy -n 20" >&2
  exit 1
fi
EOT

put /usr/local/sbin/agent-logout-all 750 root:root <<'EOT'
#!/usr/bin/env bash
set -euo pipefail
ENVF=/etc/agent-system/secrets/oauth2-proxy.env
[ "$(id -u)" = 0 ] || { echo "запускать так: sudo agent-logout-all" >&2; exit 1; }
[ -f "$ENVF" ] || { echo "секретов ещё нет (сначала agent-oauth-secret)" >&2; exit 1; }
umask 077
tmp=$(mktemp -p /etc/agent-system/secrets)
new=$(openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_')
{ grep -v '^OAUTH2_PROXY_COOKIE_SECRET=' "$ENVF" || true; printf 'OAUTH2_PROXY_COOKIE_SECRET=%s\n' "$new"; } > "$tmp"
chown root:root "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENVF"
systemctl restart oauth2-proxy
if systemctl is-active --quiet caddy; then
  systemctl restart caddy
fi
echo "все входы сброшены, открытые соединения закрыты"
EOT

secrets_ready() {
  [ -f "$ENVF" ] && grep -q '^OAUTH2_PROXY_CLIENT_ID=.\+' "$ENVF" && grep -q '^OAUTH2_PROXY_CLIENT_SECRET=.\+' "$ENVF" && grep -q '^OAUTH2_PROXY_COOKIE_SECRET=.\+' "$ENVF"
}

if ! systemctl is-enabled --quiet oauth2-proxy 2>/dev/null; then
  systemctl enable oauth2-proxy >/dev/null 2>&1
  changed=1
fi

if secrets_ready; then
  if ! systemctl is-active --quiet oauth2-proxy || [ "$restart" = 1 ]; then
    systemctl restart oauth2-proxy
    sleep 3
    systemctl is-active --quiet oauth2-proxy || { echo "oauth2-proxy не запустился" >&2; exit 1; }
    changed=1
  fi
  ss -ltnpH | grep '"oauth2-proxy"' | awk '{print $4}' | grep -qx '127.0.0.1:4180' || { echo "oauth2-proxy не слушает 127.0.0.1:4180" >&2; exit 1; }
  echo "oauth2-proxy работает на 127.0.0.1:4180"
else
  if systemctl is-active --quiet oauth2-proxy; then
    systemctl stop oauth2-proxy
  fi
  echo "секретов Google ещё нет: служба не запущена (так задумано). Дальше: шаг 4 (Google Cloud), шаг 5 (ssh -t vps sudo agent-oauth-secret)"
fi
echo "адрес возврата для Google: https://$HOST/oauth2/callback"
[ "$changed" = 1 ] && echo "готово" || echo "изменений нет"

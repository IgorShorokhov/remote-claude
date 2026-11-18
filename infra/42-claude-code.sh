#!/usr/bin/env bash
set -euo pipefail
note() { echo "[42-claude-code] $*"; }
die() { echo "[42-claude-code] ОШИБКА: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "нужен root (sudo bash -s)"

CHANGED=0
H=/home/claude
ENVFILE=/etc/agent-system/proxy.env
MS=/etc/claude-code/managed-settings.json
BIN=$H/.local/bin/claude

id claude >/dev/null 2>&1 || die "нет пользователя claude"
[ -f "$ENVFILE" ] || die "нет $ENVFILE (сначала 40-http-proxy.sh)"
set -a; . "$ENVFILE"; set +a
HTTPS_PROXY=${HTTPS_PROXY:-}; HTTP_PROXY=${HTTP_PROXY:-}; https_proxy=${https_proxy:-}; http_proxy=${http_proxy:-}
NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,::1}; no_proxy=${no_proxy:-$NO_PROXY}
case "$HTTPS_PROXY" in http://127.0.0.1:*) ;; "") note "без прокси: сервер ходит к Anthropic напрямую" ;; *) die "неожиданный HTTPS_PROXY" ;; esac
CURLPROXY=(); [ -n "$HTTPS_PROXY" ] && CURLPROXY=(-x "$HTTPS_PROXY")

if ! dpkg -s tmux >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux >/dev/null
  CHANGED=1; note "tmux установлен"
fi

if [ ! -d "$H/work" ] || [ "$(stat -c '%a %U' "$H/work")" != "700 claude" ]; then
  install -d -m 700 -o claude -g claude "$H/work"
  CHANGED=1; note "создан/исправлен ~/work (700)"
fi

if sudo -u claude -H env "HTTPS_PROXY=$HTTPS_PROXY" "$BIN" --version >/dev/null 2>&1; then
  note "Claude Code уже установлен: $(sudo -u claude -H "$BIN" --version 2>/dev/null)"
else
  d=$(mktemp -d)
  trap 'rm -rf "$d"' EXIT
  chmod 755 "$d"
  curl -fsSL -m 90 "${CURLPROXY[@]}" -o "$d/install.sh" https://claude.ai/install.sh || die "не скачался install.sh через прокси"
  head -c 2 "$d/install.sh" | grep -q '#!' || die "install.sh не похож на скрипт"
  grep -q 'DOWNLOAD_BASE_URL="https://downloads.claude.ai/claude-code-releases"' "$d/install.sh" || die "в install.sh другой адрес загрузки; остановлено"
  grep -q 'checksum_matches' "$d/install.sh" || die "в install.sh нет сверки контрольной суммы; остановлено"
  chmod 644 "$d/install.sh"
  sudo -u claude -H env "HTTPS_PROXY=$HTTPS_PROXY" "HTTP_PROXY=$HTTP_PROXY" "https_proxy=$https_proxy" "http_proxy=$http_proxy" "NO_PROXY=$NO_PROXY" "no_proxy=$no_proxy" DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 bash "$d/install.sh" stable
  CHANGED=1; note "Claude Code установлен"
fi

ver=$(sudo -u claude -H "$BIN" --version 2>/dev/null) || die "claude --version не работает"
note "версия: $ver; путь: $(readlink -f "$BIN")"

if [ "$(stat -c %a "$H/.claude" 2>/dev/null || echo none)" != 700 ]; then
  install -d -m 700 -o claude -g claude "$H/.claude"; CHANGED=1; note "~/.claude -> 700"
fi
for f in "$H/.claude.json" "$H/.claude/.credentials.json"; do
  if [ -f "$f" ] && [ "$(stat -c '%a %U' "$f")" != "600 claude" ]; then
    chown claude:claude "$f"; chmod 600 "$f"; CHANGED=1; note "права $f -> 600"
  fi
done

tmp=$(mktemp)
export HTTPS_PROXY NO_PROXY
python3 - "$tmp" <<'PY'
import json, os, sys
e = os.environ
proxy = e.get("HTTPS_PROXY", "")
no = e.get("NO_PROXY", "")
env = {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy, "NO_PROXY": no, "no_proxy": no} if proxy else {}
cfg = {
    "env": {
        **env,
        "DISABLE_ERROR_REPORTING": "1",
    },
    "permissions": {
        "disableBypassPermissionsMode": "disable",
        "deny": [
            "Read(**/.env)", "Read(**/.env.*)",
            "Read(~/.claude/.credentials.json)",
            "Read(~/.ssh/**)",
            "Read(//etc/agent-system/**)",
            "Read(~/.config/**/Login Data*)",
        ],
    },
    "autoUpdatesChannel": "stable",
    "disableRemoteControl": True,
}
with open(sys.argv[1], "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
python3 -m json.tool "$tmp" >/dev/null || die "managed-settings: неверный JSON"
if [ ! -f "$MS" ] || ! cmp -s "$tmp" "$MS" || [ "$(stat -c '%a %U' "$MS")" != "644 root" ]; then
  install -D -m 644 -o root -g root "$tmp" "$MS"
  CHANGED=1; note "записан $MS"
fi
rm -f "$tmp"
python3 -m json.tool "$MS" >/dev/null || die "$MS: неверный JSON"

if [ "$CHANGED" = 0 ]; then note "изменений нет"; else note "готово"; fi

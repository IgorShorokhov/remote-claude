#!/usr/bin/env bash
# Проверка сервера Remote Claude. Каждая строка: OK, ЖДЁТ (нужен человек) или ОШИБКА (что-то сломано). Запуск: sudo ./install.sh doctor
set -uo pipefail
cd "$(dirname "$0")/.."
env=$(python3 -I -B kit/configure.py --env) || exit 1
eval "$env"
HOST=$KIT_DOMAIN
[ "$KIT_SITE_MODE" = tailscale ] && HOST=$(cat /etc/agent-system/kit-host 2>/dev/null || true)
bad=0; wait=0; todo=()
ok() { printf '\033[32mOK\033[0m      %s\n' "$*"; }
fail() { printf '\033[31mОШИБКА\033[0m  %s\n' "$*"; bad=1; }
human() { printf '\033[33mЖДЁТ\033[0m    %s\n' "$1"; wait=1; todo+=("$2"); }
active() { systemctl is-active --quiet "$1"; }
service() { if active "$1"; then ok "служба $1 работает"; else fail "служба $1 не работает: journalctl -u $1 -n 30"; fi; }

for s in agent-tmux agent-host agent-portal caddy; do service "$s"; done
[ "$KIT_SITE_MODE" = domain ] && service agent-front
[ "$KIT_SITE_MODE" = tailscale ] && service tailscaled
[ "$KIT_FEATURE_VOICE" = yes ] && service agent-speech
[ "$KIT_FEATURE_ACCESS" = yes ] && { systemctl is-active --quiet agent-access.path && ok "мост страницы Access работает" || fail "мост Access не работает: sudo bash infra/59-access-bridge.sh"; }
[ "$KIT_BACKUPS" = yes ] && { systemctl is-active --quiet remote-claude-backup.timer && ok "ночные копии включены" || fail "таймер копий выключен: sudo bash kit/backup.sh install"; }
if [ "$KIT_PROXY" = warp ]; then
  r=$(curl -s -m 20 --proxy http://127.0.0.1:40000 https://claude.ai/cdn-cgi/trace | grep '^warp=' || true)
  [ "$r" = warp=on ] && ok "claude.ai открывается через WARP" || fail "WARP не работает: sudo bash infra/40-http-proxy.sh check"
fi

code=$(curl -s -m 5 --unix-socket /run/agent-portal/portal.sock -o /dev/null -w '%{http_code}' http://portal/api/health || true)
[ "$code" = 200 ] && ok "портал отвечает" || fail "портал не отвечает (код $code)"
feat=$(grep '^PORTAL_FEATURES=' /etc/agent-system/portal.env 2>/dev/null | cut -d= -f2-)
[ "$feat" = "$KIT_PORTAL_FEATURES" ] && ok "разделы: $KIT_SECTIONS" || fail "разделы портала ($feat) не совпадают с kit.conf: sudo ./install.sh step portal"
grep -qx "$KIT_OWNER_EMAIL admin owner" /etc/agent-system/roles.txt 2>/dev/null && ok "владелец $KIT_OWNER_EMAIL в списке почт" || fail "владельца нет в списке почт: sudo ./install.sh step web"

if [ -n "$HOST" ]; then
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$HOST/" || true)
  case "$code" in
    302) ok "сайт https://$HOST открыт и просит вход" ;;
    000) fail "https://$HOST не открывается (сеть, сертификат или порты 80/443)" ;;
    *) fail "https://$HOST отвечает $code вместо 302 (входа)" ;;
  esac
else
  fail "адрес сайта неизвестен: sudo ./install.sh step network"
fi

if grep -q '^OAUTH2_PROXY_CLIENT_SECRET=.' /etc/agent-system/secrets/oauth2-proxy.env 2>/dev/null; then
  active oauth2-proxy && ok "вход через Google настроен" || fail "oauth2-proxy не работает: journalctl -u oauth2-proxy -n 30"
else
  human "вход через Google ещё не настроен" "Google: создать OAuth-клиент (AGENT.md, «Вход через Google»), адрес возврата https://$HOST/oauth2/callback, потом в SSH самому: sudo ./install.sh google"
fi
if [ -s /home/claude/.claude/.credentials.json ]; then
  ok "Claude Code вошёл в аккаунт Claude"
else
  human "Claude Code ещё не вошёл в аккаунт Claude" "Claude: в SSH самому: sudo ./install.sh claude-login, войти подпиской Pro или Max, после входа /exit"
fi

echo
if [ "$bad" = 1 ]; then
  echo "Итог: есть ОШИБКИ. Покажите этот вывод человеку; повторный sudo ./install.sh часто их исправляет."
  exit 1
fi
if [ "$wait" = 1 ]; then
  echo "Итог: сервер готов, осталось сделать человеку:"
  for t in "${todo[@]}"; do echo "  - $t"; done
  exit 3
fi
echo "Итог: всё готово. Откройте https://$HOST и войдите почтой $KIT_OWNER_EMAIL"

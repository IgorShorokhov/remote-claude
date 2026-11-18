#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
MODE=${1:-apply}
ARG=${2:-}
ACCESSOFF=/etc/agent-system/caddy-accesslog.off
ACCESSLOG_FILE=/var/log/caddy/access.log
CADDY_BIN=${CADDY_BIN:-/usr/bin/caddy}
changed=0
reload=0
restart=0
LASTPUT=0

KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
KEY_URL=https://dl.cloudsmith.io/public/caddy/stable/gpg.key
KEY_FPR=65760C51EDEA2017CEA2CA15155B6D79CA56EA34
LIST=/etc/apt/sources.list.d/caddy-stable.list
UU=/etc/apt/apt.conf.d/52agent-caddy-updates
SEC=/etc/agent-system/secrets
DROPIN=/etc/systemd/system/caddy.service.d/agent.conf

logblock() {
  cat <<EOT
	log {
		output file $1 {
			mode 0640
			roll_size 10MiB
			roll_keep 3
			roll_keep_for 72h
		}
		format filter {
			wrap json
			fields {
				request>remote_ip delete
				request>remote_port delete
				request>client_ip delete
				request>tls delete
				user_id delete
				request>uri regexp \?.* ""
				request>headers>Authorization delete
				request>headers>X-Forwarded-Email delete
				request>headers>X-Auth-Request-Email delete
				request>headers>X-Auth-Request-User delete
				request>headers>X-Real-Ip delete
				request>headers>X-Forwarded-For delete
				request>headers>X-Forwarded-Proto delete
				request>headers>X-Forwarded-Host delete
				request>headers>X-Test-Email delete
				request>headers>User-Agent delete
				request>headers>Accept-Language delete
				request>headers>Accept-Encoding delete
				request>headers>Upgrade-Insecure-Requests delete
				request>headers>Priority delete
				request>headers>Te delete
				request>headers>Dnt delete
				request>headers>Sec-Gpc delete
				request>headers>Cache-Control delete
				request>headers>Pragma delete
				request>headers>Sec-Websocket-Key delete
				request>headers>Sec-Websocket-Extensions delete
				request>headers>Sec-Websocket-Version delete
				request>headers>Cookie replace "[есть]"
				request>headers>Referer regexp ^(?:https?://[^/?]*)?([^?#]*).*\$ \$1
				request>headers>Accept regexp ^(.{0,40}).*\$ \$1
				resp_headers>Set-Cookie replace "[set]"
				resp_headers>Location regexp ^(?:https?://([^/?]+))?([^?#]*).*\$ \$1\$2
				resp_headers>Content-Security-Policy delete
				resp_headers>Strict-Transport-Security delete
				resp_headers>Permissions-Policy delete
				resp_headers>Referrer-Policy delete
				resp_headers>X-Content-Type-Options delete
				resp_headers>X-Robots-Tag delete
				resp_headers>Cross-Origin-Embedder-Policy delete
				resp_headers>Cross-Origin-Opener-Policy delete
				resp_headers>Etag delete
				resp_headers>Last-Modified delete
			}
		}
	}
EOT
}

caddyfile() {
  local out
  out=$(cat <<'EOT' | sed -e "s|@LISTEN@|$LISTEN|g" -e "s|@AUTH@|$AUTH|g" -e "s|@ROLES@|$ROLES|g" \
      -e "s|@PORTAL@|$PORTAL|g" -e "s|@KASM_ADMIN@|$KASM_ADMIN|g" -e "s|@KASM_CHAT@|$KASM_CHAT|g" \
      -e "s|@ADMIN@|$ADMIN|g"
{
	auto_https off
	admin @ADMIN@
	default_bind 127.0.0.1
	servers {
		trusted_proxies static 127.0.0.1/32 ::1/128
	}
}

(unauth_reply) {
		route {
			@isframe header Sec-Fetch-Dest iframe
			@isnav {
				not header_regexp Sec-Fetch-Dest ^(iframe|empty|image|script|style|font|audio|video|worker|sharedworker|serviceworker|manifest|object|embed|track|xslt|report|paintworklet|audioworklet|fencedframe|frame)$
			}
			header @isframe Content-Type "text/html; charset=utf-8"
			header @isframe Content-Security-Policy "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'self' vscode-webview: vscode-file: https://{env.PORTAL_HOST}"
			respond @isframe "<!doctype html><meta charset=utf-8><body style=\"font-family:sans-serif;padding:1em\">Сессия входа истекла. Обновите страницу портала.<script>try{parent.postMessage({type:'portal-auth-expired'},'*')}catch(e){}</script>" 401
			@isnav_safe {
				not header_regexp Sec-Fetch-Dest ^(iframe|empty|image|script|style|font|audio|video|worker|sharedworker|serviceworker|manifest|object|embed|track|xslt|report|paintworklet|audioworklet|fencedframe|frame)$
				path_regexp ^/[A-Za-z0-9._~/-]*$
			}
			redir @isnav_safe "/oauth2/start?rd={http.request.uri.path}" 302
			redir @isnav "/oauth2/start?rd=%2F" 302
			header Content-Type "application/json"
			respond `{"detail":"unauthorized"}` 401
		}
}

(portal_proxy) {
	@notview expression `!(path_regexp('^/api/chats/[^/]+/outbox/[^/]+$') && {http.request.uri.query.view} == '1')`
	header @notview {
		defer
		Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' wss://{env.PORTAL_HOST}; frame-src 'self'; frame-ancestors 'self' vscode-webview: vscode-file:; object-src 'none'; base-uri 'none'"
	}
	encode zstd gzip
	request_header -Cookie
	reverse_proxy @PORTAL@
}

http://:@LISTEN@ {
	bind 127.0.0.1
@ACCESSLOG@

	route {
		request_header -Authorization
		request_header -X-Forwarded-Email
		request_header -X-Forwarded-User
		request_header -X-Forwarded-Groups
		request_header -X-Forwarded-Preferred-Username
		request_header -X-Forwarded-Access-Token
		request_header -X-Auth-Request-Email
		request_header -X-Auth-Request-User
		request_header -X-Auth-Request-Groups
		request_header -X-Auth-Request-Preferred-Username
		request_header -X-Auth-Request-Access-Token
		request_header -X-Auth-Request-Redirect
		request_header -X-Portal-Role
		request_header -X-Ext-Internal

		header {
			defer
			-Server
			Strict-Transport-Security "max-age=31536000"
			X-Content-Type-Options nosniff
			Referrer-Policy no-referrer
			X-Robots-Tag "noindex, nofollow"
			Permissions-Policy "camera=(), microphone=(self), geolocation=()"
		}

		@signin_sub {
			path /oauth2/sign_in
			header_regexp Sec-Fetch-Dest .+
			not header_regexp Sec-Fetch-Dest ^document$
		}
		handle @signin_sub {
			header Content-Type "application/json"
			respond `{"detail":"unauthorized"}` 401
		}

		handle /oauth2/* {
			header {
				defer
				Content-Security-Policy "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self' https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'"
			}
			reverse_proxy @AUTH@ {
				header_up X-Real-IP {client_ip}
				header_up X-Forwarded-Uri {uri}
			}
		}

		@ext_public path /ext/device /ext/poll /ext/ticket /ext/revoke /ext/enter /ext/bridge
		handle @ext_public {
			import portal_proxy
		}

		handle {
			route {
				@ext_cookie header_regexp Cookie (?:^|;\s*)portal_ext=
				@oauth_cookie not header_regexp Cookie (?:^|;\s*)portal_ext=
				route @ext_cookie {
					reverse_proxy @PORTAL@ {
						method GET
						rewrite /ext/verify
						header_up X-Ext-Internal 1
						@authok status 2xx
						handle_response @authok {
							request_header X-Forwarded-Email {rp.header.X-Auth-Request-Email}
						}
						@unauth status 401
						handle_response @unauth {
							import unauth_reply
						}
					}
				}
				route @oauth_cookie {
					reverse_proxy @AUTH@ {
						method GET
						rewrite /oauth2/auth
						header_up X-Real-IP {client_ip}
						header_up X-Forwarded-Method {method}
						header_up X-Forwarded-Uri {uri}
						@authok status 2xx
						handle_response @authok {
							request_header X-Forwarded-Email {rp.header.X-Auth-Request-Email}
						}
						@unauth status 401
						handle_response @unauth {
							import unauth_reply
						}
					}
				}

				import @ROLES@

				@badorigin_mut {
					not method GET HEAD
					not header Origin https://{env.PORTAL_HOST}
				}
				handle @badorigin_mut {
					respond "forbidden" 403
				}

				@badorigin_ws {
					header_regexp Upgrade (?i)websocket
					not header Origin https://{env.PORTAL_HOST}
				}
				handle @badorigin_ws {
					respond "forbidden" 403
				}

				@badorigin_conn {
					header_regexp Connection (?i)upgrade
					not header Origin https://{env.PORTAL_HOST}
				}
				handle @badorigin_conn {
					respond "forbidden" 403
				}

				request_header X-Portal-Role {portal_role}

				@role_any vars_regexp {portal_role} ^(admin|user)$
				@role_admin vars {portal_role} admin

				handle_path /desktop/chat/* {
					handle @role_any {
						# The owner can close the Chat section for an account on the Access page (decision 229): the portal answers 204 or 403.
						forward_auth @PORTAL@ {
							uri /api/access/gate/chat
							header_up -Upgrade
							header_up -Connection
							header_up -Cookie
							header_up -Authorization
						}
						request_header Authorization {env.KASM_CHAT_BASIC}
						request_header -Cookie
						header {
							defer
							Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self' data: blob: wss://{env.PORTAL_HOST}; frame-ancestors 'self' https://{env.PORTAL_HOST}; object-src 'none'; base-uri 'none'"
						}
						reverse_proxy @KASM_CHAT@ {
							header_down -WWW-Authenticate
							header_down -X-Frame-Options
							header_down -Frame-Options
							header_down -Content-Security-Policy
							header_down -Cross-Origin-Embedder-Policy
							header_down -Cross-Origin-Opener-Policy
							header_down -Cross-Origin-Resource-Policy
						}
					}
					respond "forbidden" 403
				}

				handle_path /desktop/admin/* {
					handle @role_admin {
						request_header Authorization {env.KASM_ADMIN_BASIC}
						request_header -Cookie
						header {
							defer
							Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self' data: blob: wss://{env.PORTAL_HOST}; frame-ancestors 'self' https://{env.PORTAL_HOST}; object-src 'none'; base-uri 'none'"
						}
						reverse_proxy @KASM_ADMIN@ {
							header_down -WWW-Authenticate
							header_down -X-Frame-Options
							header_down -Frame-Options
							header_down -Content-Security-Policy
							header_down -Cross-Origin-Embedder-Policy
							header_down -Cross-Origin-Opener-Policy
							header_down -Cross-Origin-Resource-Policy
						}
					}
					respond "forbidden" 403
				}

				@portal_common {
					vars_regexp {portal_role} ^(admin|user)$
					path /
					path /static/*
				}
				@portal_me {
					vars_regexp {portal_role} ^(admin|user)$
					method GET
					path /api/me
				}
				@portal_admin {
					vars {portal_role} admin
					path /api/*
				}
				@portal_user {
					vars {portal_role} user
					path /api/models /api/usage /api/sessions /api/sessions/* /api/chats /api/chats/* /api/history /api/history/* /api/personal/* /api/prompts /api/prompts/* /api/speech /api/uploads /api/uploads/* /api/dirs /api/term/* /api/presence /api/devices /api/devices/*
				}

				@portal_ext_sso {
					vars_regexp {portal_role} ^(admin|user)$
					path /ext/authorize /ext/approve
				}

				handle @portal_common {
					import portal_proxy
				}
				handle @portal_ext_sso {
					import portal_proxy
				}
				handle @portal_me {
					import portal_proxy
				}
				handle @portal_admin {
					import portal_proxy
				}
				handle @portal_user {
					import portal_proxy
				}

				respond "forbidden" 403
			}
		}
	}
}
EOT
)
  out=${out//@ACCESSLOG@/${LOGBLOCK:-}}
  printf '%s\n' "$out"
}

roles_empty() {
  cat <<'EOT'
map {http.request.header.X-Forwarded-Email} {portal_role} {
	default none
}
EOT
}

put() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  LASTPUT=0
  if [ ! -f "$dest" ] || ! cmp -s "$tmp" "$dest" || [ "$(stat -c %a "$dest")" != "$mode" ] || [ "$(stat -c %U:%G "$dest")" != "$owner" ]; then
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    changed=1
    reload=1
    LASTPUT=1
  fi
  rm -f "$tmp"
}

portal_host() {
  local h
  if [ -n "${KIT_PORTAL_HOST:-}" ]; then
    [[ "$KIT_PORTAL_HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "KIT_PORTAL_HOST: неверное имя" >&2; return 1; }
    echo "$KIT_PORTAL_HOST"
    return
  fi
  h=$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
  case "$h" in
    *.ts.net) echo "$h" ;;
    *) echo "нет имени *.ts.net у этого узла (MagicDNS выключен?)" >&2; return 1 ;;
  esac
}

selftest() {
  local fails=0 total=0 out; T=""; pids=""
  T=$(mktemp -d)
  trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; rm -rf "$T"' EXIT
  command -v python3 >/dev/null
  [ -x "$CADDY_BIN" ]

  cat > "$T/stub.py" <<'EOT'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
name, port = sys.argv[1], int(sys.argv[2])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def handle_any(self):
        if name == "portal" and self.path.split("?")[0] == "/oauth2/auth":
            e = self.headers.get("X-Test-Email")
            if e:
                self.send_response(202)
                self.send_header("X-Auth-Request-Email", e)
                if not self.headers.get("X-Test-NoCookie"):
                    self.send_header("Set-Cookie", "__Host-oauth2=refreshed; Path=/; Secure; HttpOnly")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        if name == "portal" and self.path.split("?")[0] == "/ext/verify":
            cookie = self.headers.get("Cookie") or ""
            who = "admin" if "portal_ext=good;" in cookie + ";" else "user" if "portal_ext=gooduser;" in cookie + ";" else ""
            if who and self.headers.get("X-Ext-Internal") == "1":
                self.send_response(202)
                self.send_header("X-Auth-Request-Email", who + "@test.invalid")
            else:
                self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if name == "portal" and self.path.split("?")[0] == "/api/access/gate/chat":
            leaked = any(self.headers.get(k) for k in ("Upgrade", "Authorization", "Cookie"))
            who = self.headers.get("X-Forwarded-Email") or ""
            role = self.headers.get("X-Portal-Role") or ""
            self.send_response(500 if leaked or role not in ("admin", "user") else 403 if who == "closed@test.invalid" else 204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if name == "portal" and self.path.split("?")[0] == "/oauth2/callback":
            self.send_response(302)
            self.send_header("Set-Cookie", "__Host-oauth2=SECRETSETCOOKIEVALUE; Path=/; Secure")
            self.send_header("Location", "https://accounts.example.invalid/o/auth?state=SECRETSTATE&client_id=X")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        h = {k: self.headers.get(k) for k in ("Authorization", "X-Portal-Role", "X-Forwarded-Email", "Cookie", "X-Auth-Request-User", "X-Ext-Internal")}
        body = (name + " " + json.dumps(h)).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "stub")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("WWW-Authenticate", 'Basic realm="x"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = handle_any
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOT
  for spec in portal:18081 kasm-admin:18444 kasm-chat:18445; do
    python3 -I "$T/stub.py" "${spec%%:*}" "${spec##*:}" &
    pids="$pids $!"
  done

  cat > "$T/roles.caddy" <<'EOT'
map {http.request.header.X-Forwarded-Email} {portal_role} {
	~(?i)^admin@test\.invalid$ admin
	~(?i)^user@test\.invalid$ user
	~(?i)^closed@test\.invalid$ user
	default none
}
EOT
  LOGBLOCK=$(logblock "$T/access.log") LISTEN=18080 AUTH=127.0.0.1:18081 ROLES=$T/roles.caddy PORTAL=127.0.0.1:18081 \
    KASM_ADMIN=127.0.0.1:18444 KASM_CHAT=127.0.0.1:18445 ADMIN=off caddyfile > "$T/Caddyfile"
  XDG_CONFIG_HOME=$T/cfg XDG_DATA_HOME=$T/data HOME=$T \
    PORTAL_HOST=portal.test.invalid KASM_ADMIN_BASIC="Basic test-admin" KASM_CHAT_BASIC="Basic test-chat" \
    "$CADDY_BIN" run --config "$T/Caddyfile" --adapter caddyfile >"$T/caddy.log" 2>&1 &
  pids="$pids $!"

  local i
  for i in $(seq 1 50); do
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:18080/oauth2/x && break
    sleep 0.2
  done
  if ! curl -s -o /dev/null --max-time 1 http://127.0.0.1:18080/oauth2/x; then
    echo "selftest не пройден: временный Caddy не стартовал" >&2
    grep -iE 'error|invalid|unrecognized|fail' "$T/caddy.log" | cut -c1-400 >&2
    exit 1
  fi

  local B=http://127.0.0.1:18080 OK=https://portal.test.invalid
  check() {
    local want=$1 label=$2 who=$3 method=$4 path=$5; shift 5
    local args=() code
    [ "$who" = none ] || args+=(-H "X-Test-Email: $who@test.invalid")
    code=$(curl -s -o "$T/body" -w '%{http_code}' --max-time 5 --path-as-is -X "$method" "${args[@]}" "$@" "$B$path" || true)
    total=$((total+1))
    if [ "$code" != "$want" ]; then
      fails=$((fails+1))
      echo "ОШИБКА: $label ($who $method $path) ждали $want, получили $code"
    fi
  }
  expect_body() {
    local label=$1 pattern=$2
    total=$((total+1))
    if ! grep -qE -- "$pattern" "$T/body"; then
      fails=$((fails+1))
      echo "ОШИБКА: $label"
    fi
  }
  deny_body() {
    local label=$1 pattern=$2
    total=$((total+1))
    if grep -qE -- "$pattern" "$T/body"; then
      fails=$((fails+1))
      echo "ОШИБКА: $label"
    fi
  }

  check 302 "без входа /" none GET /
  check 302 "без входа /api/me" none GET /api/me
  check 302 "без входа /api/devices" none GET /api/devices
  check 302 "без входа экран админа" none GET /desktop/admin/
  check 302 "без входа экран чата" none GET /desktop/chat/
  check 200 "oauth2 доступен без входа" none GET /oauth2/sign_in
  for who in other; do
    check 403 "без роли /" $who GET /
    check 403 "без роли /api/me" $who GET /api/me
    check 403 "без роли экран чата" $who GET /desktop/chat/
    check 403 "без роли экран админа" $who GET /desktop/admin/
  done
  check 200 "user /" user GET /
  check 200 "user /static" user GET /static/app.js
  check 200 "user /api/me" user GET /api/me
  check 403 "user /api/me POST" user POST /api/me -H "Origin: $OK"
  check 403 "user /api/term" user GET /api/term
  check 200 "user /api/sessions" user GET /api/sessions
  check 200 "user /api/chats" user GET /api/chats
  check 200 "user /api/devices" user GET /api/devices
  check 200 "user скачивает установщик" user GET /api/devices/download/windows
  check 200 "user DELETE /api/devices/<id>" user DELETE /api/devices/0123abcd -H "Origin: $OK"
  check 403 "user /api/devicesx" user GET /api/devicesx
  check 200 "user /api/history" user GET /api/history/abc
  check 200 "user /api/term с именем" user GET /api/term/main
  check 200 "user POST /api/chats" user POST /api/chats -H "Origin: $OK"
  check 403 "user POST /api/chats без Origin" user POST /api/chats
  check 403 "user /api/pc" user GET /api/pc
  check 403 "user /api/sync" user GET /api/sync
  check 403 "user /api/vpn" user GET /api/vpn
  check 403 "user /api/vpn POST" user POST /api/vpn/refresh -H "Origin: $OK"
  check 403 "user /api/backup" user GET /api/backup
  check 403 "user /api/backup/status" user GET /api/backup/status
  check 403 "user POST /api/consoles" user POST /api/consoles -H "Origin: $OK"
  check 403 "user /api/presence/state" user GET /api/presence/state
  check 403 "user /api/sessionsx" user GET /api/sessionsx
  check 403 "user двойной слеш /api/pc" user GET //api/pc
  check 403 "user обход через .." user GET /api/sessions/../pc
  check 200 "user POST /api/presence" user POST /api/presence -H "Origin: $OK"
  check 200 "user экран чата" user GET /desktop/chat/
  check 200 "user экран чата, глубокий путь" user GET /desktop/chat/websockify
  check 403 "user экран админа" user GET /desktop/admin/
  check 403 "закрытый раздел Chat: экран чата" closed GET /desktop/chat/
  check 403 "закрытый раздел Chat: глубокий путь" closed GET /desktop/chat/vnc.html
  check 403 "закрытый раздел Chat: WebSocket со своим Origin" closed GET /desktop/chat/websockify -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: $OK"
  deny_body "закрытый раздел Chat получил экран" 'kasm-chat'
  check 200 "закрытый раздел Chat: остальной портал" closed GET /api/me
  check 200 "открытый Chat: WebSocket экрана со своим Origin" user GET /desktop/chat/websockify -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: $OK" -H "Authorization: Basic cHdu" -H "Cookie: a=b"
  expect_body "открытый Chat: экран получил пароль Caddy" '"Authorization": "Basic test-chat"'
  check 403 "user экран админа, глубокий путь" user GET /desktop/admin/websockify
  check 403 "user прочее" user GET /evil
  check 403 "user двойной слеш" user GET //api/term
  check 403 "user обход через .." user GET /desktop/chat/%2e%2e/admin/
  deny_body "user получил экран админа через .." 'kasm-admin'
  check 403 "user обход через ..%2f" user GET "/desktop/chat/..%2fadmin/"
  deny_body "user получил экран админа через ..%2f" 'kasm-admin'
  check 200 "admin /" admin GET /
  check 200 "admin /api/me" admin GET /api/me
  check 200 "admin /api/term" admin GET /api/term
  check 200 "admin экран чата" admin GET /desktop/chat/
  check 200 "admin экран админа" admin GET /desktop/admin/
  check 403 "admin прочее" admin GET /evil
  check 403 "admin корень /desktop/" admin GET /desktop/
  check 403 "admin POST без Origin" admin POST /api/term
  check 403 "admin POST чужой Origin" admin POST /api/term -H "Origin: https://evil.invalid"
  check 403 "admin POST Origin http" admin POST /api/term -H "Origin: http://portal.test.invalid"
  check 200 "admin POST свой Origin" admin POST /api/term -H "Origin: $OK"
  check 403 "admin DELETE без Origin" admin DELETE /api/term
  check 403 "admin WebSocket чужой Origin" admin GET /api/term -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: https://evil.invalid"
  check 403 "admin WebSocket без Origin" admin GET /api/term -H "Upgrade: WebSocket" -H "Connection: Upgrade"
  check 200 "admin WebSocket свой Origin" admin GET /api/term -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: $OK"
  check 403 "admin WebSocket экран с чужим Origin" admin GET /desktop/admin/websockify -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: https://evil.invalid"
  check 403 "user WebSocket чата с чужим Origin" user GET /desktop/chat/websockify -H "Upgrade: websocket" -H "Connection: Upgrade" -H "Origin: https://evil.invalid"

  check 200 "user в другом регистре: /api/me" none GET /api/me -H "X-Test-Email: User@Test.INVALID"
  check 403 "user в другом регистре: /api/pc" none GET /api/pc -H "X-Test-Email: USER@test.invalid"
  check 200 "admin в другом регистре: /api/term" none GET /api/term -H "X-Test-Email: ADMIN@Test.Invalid"
  check 403 "почта с лишним началом" none GET /api/term -H "X-Test-Email: xadmin@test.invalid"
  check 403 "почта с лишним концом" none GET /api/term -H "X-Test-Email: admin@test.invalid.evil"
  check 403 "точка в почте не любой символ" none GET /api/term -H "X-Test-Email: admin@testXinvalid"
  check 403 "Connection: keep-alive, Upgrade без Origin" admin POST /api/term -H "Connection: keep-alive, Upgrade"
  check 403 "Connection: keep-alive, Upgrade без Origin (GET)" admin GET /api/term -H "Connection: keep-alive, Upgrade"
  check 403 "WebSocket в смешанном регистре без Origin" admin GET /api/term -H "Upgrade: WebSocket"

  check 302 "подмена заголовков без входа" none GET / -H "X-Forwarded-Email: admin@test.invalid" -H "X-Portal-Role: admin" -H "X-Auth-Request-Email: admin@test.invalid"
  check 403 "подмена почты у user" user GET /api/pc -H "X-Forwarded-Email: admin@test.invalid" -H "X-Portal-Role: admin" -H "X-Auth-Request-Email: admin@test.invalid"
  check 200 "заголовки до портала" user GET /api/me -H "X-Forwarded-Email: admin@test.invalid" -H "X-Portal-Role: admin" -H "Authorization: Basic cHdu" -H "Cookie: a=b" -H "X-Auth-Request-User: admin"
  expect_body "портал не получил роль user" '"X-Portal-Role": "user"'
  expect_body "портал не получил почту user" '"X-Forwarded-Email": "user@test.invalid"'
  deny_body "до портала дошёл Authorization клиента" 'cHdu'
  deny_body "до портала дошла кука" '"Cookie": "a=b"'
  deny_body "до портала дошёл X-Auth-Request-User" '"X-Auth-Request-User": "admin"'
  check 200 "заголовки до экрана чата" user GET /desktop/chat/ -H "Authorization: Basic cHdu" -H "Cookie: a=b"
  expect_body "Caddy не подставил пароль экрана чата" '"Authorization": "Basic test-chat"'
  deny_body "до экрана дошла кука" '"Cookie": "a=b"'
  check 200 "заголовки до экрана админа" admin GET /desktop/admin/ -H "Authorization: Basic cHdu"
  expect_body "Caddy не подставил пароль экрана админа" '"Authorization": "Basic test-admin"'

  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/desktop/admin/")
  total=$((total+1)); echo "$out" | grep -qi '^www-authenticate' && { fails=$((fails+1)); echo "ОШИБКА: WWW-Authenticate дошёл до браузера"; }
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/")
  total=$((total+1)); echo "$out" | grep -qi '^set-cookie:' && { fails=$((fails+1)); echo "ОШИБКА: Set-Cookie из проверки входа дошёл до браузера (передача кук отключена)"; }
  for dsk in admin chat; do
    out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/desktop/$dsk/vnc.html?autoconnect=1&path=desktop/$dsk/websockify")
    total=$((total+1)); echo "$out" | grep -q '^HTTP/1.1 200' || { fails=$((fails+1)); echo "ОШИБКА: /desktop/$dsk/vnc.html с параметрами не 200"; }
    total=$((total+1)); echo "$out" | grep -qiE '^(x-frame-options|cross-origin-embedder-policy|cross-origin-opener-policy):' && { fails=$((fails+1)); echo "ОШИБКА: /desktop/$dsk отдаёт X-Frame-Options или COEP/COOP"; }
    total=$((total+1)); [ "$(echo "$out" | grep -ci '^content-security-policy:')" = 1 ] || { fails=$((fails+1)); echo "ОШИБКА: у /desktop/$dsk не ровно один CSP"; }
    total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qi "frame-ancestors 'self' https://portal.test.invalid" || { fails=$((fails+1)); echo "ОШИБКА: у /desktop/$dsk нет frame-ancestors 'self' и адреса портала"; }
    total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qi "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'" || { fails=$((fails+1)); echo "ОШИБКА: у /desktop/$dsk нет script-src для встроенных обработчиков KasmVNC"; }
  done
  out=$(curl -s -i -H "Sec-Fetch-Dest: iframe" "$B/desktop/admin/vnc.html")
  total=$((total+1)); { echo "$out" | head -1 | grep -q ' 401' && echo "$out" | grep -q 'portal-auth-expired' && ! echo "$out" | grep -q 'top.location'; } || { fails=$((fails+1)); echo "ОШИБКА: iframe без входа должен получить страницу 401 с postMessage и без навигации top"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: script" "$B/desktop/admin/assets/x.js")
  total=$((total+1)); { echo "$out" | head -1 | grep -q ' 401' && echo "$out" | grep -qi 'application/json'; } || { fails=$((fails+1)); echo "ОШИБКА: подзапрос (script) без входа должен получить 401 JSON, а не редирект"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: empty" "$B/api/me")
  total=$((total+1)); { echo "$out" | head -1 | grep -q ' 401' && echo "$out" | grep -q 'unauthorized'; } || { fails=$((fails+1)); echo "ОШИБКА: fetch /api/me без входа должен получить 401 JSON"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: document" "$B/")
  total=$((total+1)); { echo "$out" | head -1 | grep -q ' 302' && echo "$out" | grep -qi '^location: /oauth2/start?rd=/'; } || { fails=$((fails+1)); echo "ОШИБКА: переход документа без входа должен вести на /oauth2/start"; }
  out=$(curl -s -i "$B/")
  total=$((total+1)); echo "$out" | head -1 | grep -q ' 302' || { fails=$((fails+1)); echo "ОШИБКА: запрос без Sec-Fetch-Dest и без входа должен давать 302"; }
  out=$(curl -s -D - -o /dev/null "$B/oauth2/sign_in")
  total=$((total+1)); echo "$out" | grep -qi "^content-security-policy:.*frame-ancestors 'none'" || { fails=$((fails+1)); echo "ОШИБКА: нет CSP у /oauth2/*"; }
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/")
  total=$((total+1)); echo "$out" | grep -qi "^content-security-policy:.*unsafe-inline" && { fails=$((fails+1)); echo "ОШИБКА: в CSP портала есть unsafe-inline"; }
  total=$((total+1)); echo "$out" | grep -qi '^server:' && { fails=$((fails+1)); echo "ОШИБКА: заголовок Server виден"; }
  total=$((total+1)); echo "$out" | grep -qi '^strict-transport-security:' || { fails=$((fails+1)); echo "ОШИБКА: нет HSTS"; }
  total=$((total+1)); echo "$out" | grep -qi '^content-security-policy:' || { fails=$((fails+1)); echo "ОШИБКА: нет CSP у портала"; }


  check 200 "кука расширения вместо входа Google" none GET /api/me -H "Cookie: portal_ext=good"
  expect_body "роль admin для сессии расширения" '"X-Portal-Role": "admin"'
  expect_body "почта сессии расширения до портала" '"X-Forwarded-Email": "admin@test.invalid"'
  deny_body "кука расширения дошла до портала" 'portal_ext'
  check 200 "кука расширения: страница" none GET / -H "Cookie: portal_ext=good"
  check 200 "кука расширения: экран чата" none GET /desktop/chat/ -H "Cookie: portal_ext=good"
  check 403 "кука расширения user: /api/pc" none GET /api/pc -H "Cookie: portal_ext=gooduser"
  check 200 "кука расширения user: /api/chats" none GET /api/chats -H "Cookie: portal_ext=gooduser"
  check 403 "кука расширения без Origin: запись" none POST /api/chats -H "Cookie: portal_ext=good"
  check 200 "кука расширения со своим Origin: запись" none POST /api/chats -H "Cookie: portal_ext=good" -H "Origin: $OK"
  check 401 "плохая кука расширения" none GET /api/me -H "Cookie: portal_ext=bad" -H "Sec-Fetch-Dest: empty"
  expect_body "плохая кука расширения: ответ" 'unauthorized'
  check 401 "плохая кука расширения не заменяется входом Google" user GET /api/me -H "Cookie: portal_ext=bad" -H "Sec-Fetch-Dest: empty"
  check 401 "кука расширения со спуфингом внутреннего заголовка" none GET /api/me -H "Cookie: portal_ext=bad" -H "X-Ext-Internal: 1" -H "X-Forwarded-Email: admin@test.invalid" -H "Sec-Fetch-Dest: empty"
  out=$(curl -s -i -H "Sec-Fetch-Dest: iframe" -H "Cookie: portal_ext=bad" "$B/api/me")
  total=$((total+1)); { echo "$out" | head -1 | grep -q ' 401' && echo "$out" | grep -q 'portal-auth-expired' && echo "$out" | grep -qi "frame-ancestors 'self' vscode-webview: vscode-file:"; } || { fails=$((fails+1)); echo "ОШИБКА: iframe с плохой кукой расширения должен получить страницу 401 с postMessage"; }
  check 403 "ext/verify снаружи" admin GET /ext/verify -H "X-Ext-Internal: 1"
  check 302 "ext/verify без входа" none GET /ext/verify -H "X-Ext-Internal: 1"
  for path in /ext/device /ext/poll /ext/ticket /ext/revoke /ext/bridge; do
    check 200 "открытый $path без входа" none POST $path -H "Content-Type: application/json" -H "X-Forwarded-Email: admin@test.invalid" -H "X-Portal-Role: admin" -H "X-Ext-Internal: 1" -H "Cookie: a=b" -H "Authorization: Basic cHdu"
    deny_body "до $path дошла почта" '"X-Forwarded-Email": "admin'
    deny_body "до $path дошла роль" '"X-Portal-Role": "admin'
    deny_body "до $path дошёл внутренний заголовок" '"X-Ext-Internal": "1"'
    deny_body "до $path дошла кука" '"Cookie": "a=b"'
    deny_body "до $path дошёл Authorization" 'cHdu'
  done
  check 200 "открытый ext/enter без входа" none GET "/ext/enter?ticket=x"
  check 302 "ext/authorize без входа" none GET /ext/authorize
  check 403 "ext/authorize без роли" other GET /ext/authorize
  check 200 "ext/authorize для user" user GET /ext/authorize
  check 200 "ext/approve для user с Origin" user POST /ext/approve -H "Origin: $OK"
  check 403 "ext/approve без Origin" user POST /ext/approve
  check 403 "ext/approve по GET не открыт сверх роли" other GET /ext/approve
  check 403 "ext/device по произвольному пути внутри /ext/" admin GET /ext/other
  check 200 "ext/bridge: сокет расширения Chrome (Origin chrome-extension, Upgrade) доходит до портала без входа" none GET /ext/bridge -H "Origin: chrome-extension://abcdefghijklmnopabcdefghijklmnop" -H "Connection: Upgrade" -H "Upgrade: websocket"
  deny_body "до ext/bridge дошла почта" '"X-Forwarded-Email": "admin'
  check 403 "ext/bridge/other не открыт" admin GET /ext/bridge/other -H "Origin: chrome-extension://abcdefghijklmnopabcdefghijklmnop"
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/")
  total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qi "frame-ancestors 'self' vscode-webview: vscode-file:" || { fails=$((fails+1)); echo "ОШИБКА: у портала нет frame-ancestors для VS Code"; }
  SID_T=2f3c1d2e-aaaa-4bbb-8ccc-123456789abc
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/api/chats/$SID_T/outbox/a.html?view=1")
  total=$((total+1)); echo "$out" | tr -d '\r' | grep -i '^content-security-policy:' | grep -qi "^content-security-policy: frame-ancestors 'none'$" || { fails=$((fails+1)); echo "ОШИБКА: просмотр файла outbox не сохранил CSP приложения"; }
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/api/chats/$SID_T/outbox/a.html")
  total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qi "vscode-webview" || { fails=$((fails+1)); echo "ОШИБКА: скачивание из outbox не получило CSP портала"; }
  out=$(curl -s -D - -o /dev/null -H "X-Test-Email: admin@test.invalid" "$B/api/chats/$SID_T/outbox?view=1")
  total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qi "vscode-webview" || { fails=$((fails+1)); echo "ОШИБКА: ?view=1 вне outbox-файла не должен отключать CSP портала"; }
  total=$((total+1)); echo "$out" | grep -i '^content-security-policy:' | grep -qiE "frame-ancestors[^;]*(\*|https:|http:)" && { fails=$((fails+1)); echo "ОШИБКА: frame-ancestors портала пускает любые сайты"; }

  curl -s -o /dev/null -H "X-Test-Email: secret-person@test.invalid" -H "Cookie: sess=SECRETCOOKIEVALUE" "$B/api/me?code=SECRETQUERYCODE&state=zzz"
  sleep 1
  total=$((total+1)); [ -s "$T/access.log" ] || { fails=$((fails+1)); echo "ОШИБКА: журнал доступа пуст"; }
  total=$((total+1)); { grep -q '"status"' "$T/access.log" 2>/dev/null && grep -q '/api/me' "$T/access.log"; } || { fails=$((fails+1)); echo "ОШИБКА: в журнале нет пути и статуса"; }
  total=$((total+1)); grep -qE 'SECRETCOOKIEVALUE|SECRETQUERYCODE|secret-person|code=|state=|@test\.invalid' "$T/access.log" && { fails=$((fails+1)); echo "ОШИБКА: в журнале доступа есть куки, почта или query"; grep -oE '.{40}(SECRETCOOKIEVALUE|SECRETQUERYCODE|secret-person|code=|state=|@test\.invalid).{20}' "$T/access.log" | head -3; }
  curl -s -o /dev/null -H "Sec-Fetch-Dest: document" -H "Sec-Fetch-Mode: navigate" -H "Sec-Fetch-Site: cross-site" -H "Referer: https://elsewhere.invalid/p/a/t/h?token=SECRETREFERERQUERY" -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8,EXTRAACCEPTTAIL" -H "Cookie: sess=SECRETCOOKIEVALUE2" "$B/oauth2/callback?code=SECRETCODE2&state=SECRETSTATE2"
  sleep 1
  total=$((total+1)); grep -qE 'SECRETSETCOOKIEVALUE|SECRETCOOKIEVALUE2|SECRETREFERERQUERY|SECRETSTATE|SECRETCODE2|EXTRAACCEPTTAIL|state=|client_id' "$T/access.log" && { fails=$((fails+1)); echo "ОШИБКА: в журнале значения кук, query, Referer-query или хвост Accept"; }
  total=$((total+1)); { grep -q 'Sec-Fetch-Dest' "$T/access.log" && grep -q '\[есть\]' "$T/access.log" && grep -q '\[set\]' "$T/access.log" && grep -q '/p/a/t/h' "$T/access.log"; } || { fails=$((fails+1)); echo "ОШИБКА: в журнале нет Sec-Fetch-Dest, признака куки, признака Set-Cookie или пути Referer"; }
  total=$((total+1)); grep -qiE 'authorization|x-forwarded-email|user-agent' "$T/access.log" && { fails=$((fails+1)); echo "ОШИБКА: в журнале Authorization, почта или User-Agent"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: iframe" "$B/oauth2/sign_in")
  total=$((total+1)); echo "$out" | head -1 | grep -q ' 401' || { fails=$((fails+1)); echo "ОШИБКА: /oauth2/sign_in из iframe должен получать 401 без прокси"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: document" "$B/oauth2/sign_in")
  total=$((total+1)); echo "$out" | head -1 | grep -q ' 200' || { fails=$((fails+1)); echo "ОШИБКА: /oauth2/sign_in как документ должен проксироваться"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: document" "$B/some/page")
  total=$((total+1)); echo "$out" | grep -qi '^location: /oauth2/start?rd=/some/page' || { fails=$((fails+1)); echo "ОШИБКА: переход без входа должен вести на /oauth2/start?rd=<путь>"; }
  out=$(curl -s -i -H "Sec-Fetch-Dest: document" "$B/we%26ird?x=1")
  total=$((total+1)); echo "$out" | grep -qi '^location: /oauth2/start?rd=%2F' || { fails=$((fails+1)); echo "ОШИБКА: небезопасный путь должен вести на rd=%2F"; }

  if [ "$fails" -eq 0 ]; then
    echo "selftest пройден: проверок $total"
  else
    echo "selftest НЕ пройден: ошибок $fails из $total"
    grep -iE 'error|invalid|unrecognized|fail' "$T/caddy.log" | cut -c1-400 >&2
    exit 1
  fi
}

if [ "$MODE" = selftest ]; then
  selftest
  exit 0
fi
[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }
if [ "$MODE" = accesslog ]; then
  case "$ARG" in
    on) rm -f "$ACCESSOFF" ;;
    off) install -d -m 755 /etc/agent-system; : > "$ACCESSOFF" ;;
    *) echo "режимы: accesslog on|off" >&2; exit 2 ;;
  esac
  MODE=apply
fi
[ "$MODE" = apply ] || { echo "режимы: apply, selftest, accesslog on|off" >&2; exit 2; }

HOST=$(portal_host)

if ! getent group agent-portal >/dev/null; then
  groupadd --system agent-portal
  changed=1
fi

install -d -m 755 /etc/agent-system
if [ "$(stat -c %a:%U /etc/agent-system/secrets 2>/dev/null || true)" != "700:root" ]; then
  install -d -m 700 -o root -g root /etc/agent-system/secrets
  changed=1
fi

if [ -f /etc/agent-system/portal.env ] && grep -q '^PORTAL_HOST=' /etc/agent-system/portal.env; then
  if [ "$(grep '^PORTAL_HOST=' /etc/agent-system/portal.env)" != "PORTAL_HOST=$HOST" ]; then
    sed -i "s|^PORTAL_HOST=.*|PORTAL_HOST=$HOST|" /etc/agent-system/portal.env
    changed=1
    reload=1
  fi
else
  echo "PORTAL_HOST=$HOST" >> /etc/agent-system/portal.env
  changed=1
  reload=1
fi
chmod 644 /etc/agent-system/portal.env

if [ ! -s "$KEYRING" ]; then
  d=$(mktemp -d)
  curl -fsSL "$KEY_URL" -o "$d/key"
  fpr=$(gpg --show-keys --with-colons "$d/key" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
  if [ "$fpr" != "$KEY_FPR" ]; then
    echo "отпечаток ключа Caddy не совпал, ничего не ставлю" >&2
    rm -rf "$d"
    exit 1
  fi
  gpg --dearmor < "$d/key" > "$KEYRING"
  chmod 644 "$KEYRING"
  rm -rf "$d"
  changed=1
fi

put "$LIST" 644 root:root <<'EOT'
deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main
EOT

put "$UU" 644 root:root <<'EOT'
Unattended-Upgrade::Origins-Pattern {
        "o=cloudsmith/caddy/stable,a=any-version";
};
EOT

install -d -m 755 /etc/caddy

CD=$(mktemp -d)
chmod 700 "$CD"
trap 'rm -rf "$CD"' EXIT
CF=$CD/Caddyfile
LOGBLOCK=
if [ ! -e "$ACCESSOFF" ]; then
  LOGBLOCK=$(logblock "$ACCESSLOG_FILE")
fi
LISTEN=8080 AUTH=127.0.0.1:4180 ROLES=/etc/caddy/roles.caddy PORTAL=unix//run/agent-portal/portal.sock \
  KASM_ADMIN=127.0.0.1:8444 KASM_CHAT=127.0.0.1:8445 ADMIN=unix//run/caddy/admin.sock caddyfile > "$CF"
# Keep the working file: if the new one fails validation below, it is put back, so a later restart of Caddy still starts.
[ -f /etc/caddy/Caddyfile ] && cp -p /etc/caddy/Caddyfile "$CD/Caddyfile.prev"
put /etc/caddy/Caddyfile 644 root:root < "$CF"

if [ ! -f /etc/caddy/roles.caddy ]; then
  put /etc/caddy/roles.caddy 640 root:root < <(roles_empty)
fi

if [ "$(dpkg-query -W -f='${Status}' caddy 2>/dev/null || true)" != "install ok installed" ]; then
  apt-get update -qq
  printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
  chmod 755 /usr/sbin/policy-rc.d
  trap 'rm -f /usr/sbin/policy-rc.d; rm -rf "$CD"' EXIT
  apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold caddy
  rm -f /usr/sbin/policy-rc.d
  trap 'rm -rf "$CD"' EXIT
  changed=1
  reload=1
fi

if [ "$(stat -c %U:%G:%a /etc/caddy/roles.caddy)" != "root:caddy:640" ]; then
  chown root:caddy /etc/caddy/roles.caddy
  chmod 640 /etc/caddy/roles.caddy
  changed=1
  reload=1
fi

install -d -m 750 -o caddy -g caddy /var/log/caddy
if [ -e "$ACCESSOFF" ]; then
  if ls /var/log/caddy/access*.log* >/dev/null 2>&1; then
    rm -f /var/log/caddy/access*.log*
    changed=1
  fi
else
  if [ ! -e "$ACCESSLOG_FILE" ]; then
    : > "$ACCESSLOG_FILE"
    changed=1
  fi
  chown caddy:caddy "$ACCESSLOG_FILE"
  chmod 640 "$ACCESSLOG_FILE"
fi

put "$DROPIN" 644 root:root <<'EOT'
[Service]
ExecStart=
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile
EnvironmentFile=/etc/agent-system/portal.env
EnvironmentFile=-/etc/agent-system/secrets/kasmvnc-admin.env
EnvironmentFile=-/etc/agent-system/secrets/kasmvnc-chat.env
SupplementaryGroups=agent-portal
RuntimeDirectory=caddy
RuntimeDirectoryMode=0755
MemoryMax=256M
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/caddy
ReadWritePaths=-/var/log/caddy
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
AmbientCapabilities=
CapabilityBoundingSet=
IPAddressDeny=any
IPAddressAllow=localhost
EOT
restart=$LASTPUT

if [ "$reload" = 1 ]; then
  systemctl daemon-reload
fi

if ! PORTAL_HOST=$HOST "$CADDY_BIN" validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$CD/validate.log" 2>&1; then
  tail -n 5 "$CD/validate.log" | cut -c1-200 >&2
  if [ -f "$CD/Caddyfile.prev" ]; then
    install -m 644 -o root -g root "$CD/Caddyfile.prev" /etc/caddy/Caddyfile
    echo "caddy validate не прошёл; прежний Caddyfile возвращён, работающий Caddy не тронут" >&2
  else
    echo "caddy validate не прошёл" >&2
  fi
  exit 1
fi

if ! systemctl is-enabled --quiet caddy; then
  systemctl enable caddy >/dev/null 2>&1
  changed=1
fi
if ! systemctl is-active --quiet caddy; then
  systemctl start caddy
  changed=1
elif [ "$restart" = 1 ]; then
  systemctl restart caddy
elif [ "$reload" = 1 ]; then
  if ! systemctl reload caddy 2>/dev/null; then
    systemctl restart caddy
  fi
fi

sleep 1
systemctl is-active --quiet caddy || { echo "caddy не запустился" >&2; journalctl -u caddy -n 15 --no-pager | cut -c1-200 >&2; exit 1; }

listen=$(ss -ltnpH | grep '"caddy"' | awk '{print $4}' | sort | tr '\n' ' ')
if [ "$listen" != "127.0.0.1:8080 " ]; then
  echo "caddy слушает не только 127.0.0.1:8080: $listen" >&2
  exit 1
fi

UU_OUT=$(unattended-upgrade --dry-run --debug 2>&1 || true)
if ! grep -q 'Allowed origins are:.*o=cloudsmith/caddy/stable,a=any-version' <<<"$UU_OUT"; then
  echo "unattended-upgrades не видит источник Caddy" >&2
  exit 1
fi

if [ "$changed" = 1 ]; then
  echo "готово: caddy $("$CADDY_BIN" version | cut -d' ' -f1), слушает $listen"
else
  echo "изменений нет"
fi

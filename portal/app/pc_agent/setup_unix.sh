#!/bin/sh
# Claude Portal setup for Linux and macOS. Run it as yourself (not with sudo): sh claude-portal-setup.sh
set -eu
PORTAL='@PORTAL@'
PY=$(command -v python3 || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is needed (python3 was not found or is too old)." >&2
  if [ "$(uname -s)" = Darwin ]; then
    echo "On a Mac run: xcode-select --install   (installs the Command Line Tools with python3), then run this script again." >&2
  else
    echo "Debian/Ubuntu: sudo apt install python3   Fedora: sudo dnf install python3" >&2
  fi
  exit 1
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
"$PY" -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$TMP/portal_agent.py" <<'AGENT'
@AGENT_B64@
AGENT
"$PY" "$TMP/portal_agent.py" setup --portal "$PORTAL" --label "$(uname -s) $(hostname)"

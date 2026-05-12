#!/usr/bin/env bash
# Тянет /srv/aiboard в актуальное состояние с origin/main.
# Если поменялся proxy.py — рестартит aiproxy.
# Если поменялся deploy/Caddyfile — перекладывает его в /etc/caddy и reload-ит caddy.
#
# Запускается systemd-таймером aiboard-sync.timer каждые ~15 сек.

set -euo pipefail

APP_DIR="/srv/aiboard"
cd "$APP_DIR"

# Если origin не настроен — молча выходим (сайт ещё на локальном bootstrap-коммите).
if ! git remote get-url origin >/dev/null 2>&1; then
  exit 0
fi

OLD="$(git rev-parse HEAD 2>/dev/null || echo NONE)"
git fetch --quiet origin
NEW="$(git rev-parse origin/main 2>/dev/null || echo NONE)"

if [[ "$NEW" == "NONE" || "$OLD" == "$NEW" ]]; then
  exit 0
fi

echo "[aiboard-sync] $OLD -> $NEW"
git reset --hard "$NEW"

CHANGED="$(git diff --name-only "$OLD" "$NEW" 2>/dev/null || true)"

if grep -qx 'proxy.py' <<<"$CHANGED"; then
  echo "[aiboard-sync] proxy.py changed -> restart aiproxy"
  systemctl restart aiproxy
fi

if grep -qx 'deploy/Caddyfile' <<<"$CHANGED"; then
  install -m 0644 "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
  systemctl reload caddy
  echo "[aiboard-sync] Caddyfile changed -> reload caddy"
fi

if grep -qx 'deploy/aiproxy.service' <<<"$CHANGED"; then
  install -m 0644 "$APP_DIR/deploy/aiproxy.service" /etc/systemd/system/aiproxy.service
  systemctl daemon-reload
  systemctl restart aiproxy
  echo "[aiboard-sync] aiproxy.service changed -> reloaded + restarted"
fi

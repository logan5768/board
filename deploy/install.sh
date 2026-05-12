#!/usr/bin/env bash
# Установка aiboard на Ubuntu 22.04 VDS.
#
# Использование:
#   sudo bash deploy/install.sh                                  # без GitHub-sync
#   sudo REPO_URL=git@github.com:USER/aiboard.git bash deploy/install.sh
#                                                                # с GitHub-sync
#
# Что делает:
#   1) ставит python3, git, caddy
#   2) создаёт системного юзера aiproxy
#   3) делает /srv/aiboard git-репозиторием (если он ещё не им)
#      - если задан REPO_URL: добавляет origin, делает git fetch + reset --hard origin/main
#      - иначе оставляет как локальный репо (sync-таймер ничего не будет делать,
#        пока origin не появится)
#   4) генерирует /root/.ssh/id_aiboard (deploy-key для GitHub) и печатает публичный
#      ключ — добавьте его в Settings → Deploy keys нужного репозитория (read-only)
#   5) ставит systemd-юнит aiproxy + Caddyfile
#   6) ставит таймер aiboard-sync.timer (git pull каждые 15 сек). Если REPO_URL не задан,
#      таймер всё равно крутится, но aiboard-sync.sh при отсутствии origin молча выходит.
#
# Все шаги идемпотентны — можно запускать повторно.

set -euo pipefail

REPO_URL="${REPO_URL:-}"
DOMAIN="${DOMAIN:-board.spirtvpn.ru}"
APP_DIR="/srv/aiboard"
SSH_KEY="/root/.ssh/id_aiboard"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> 1/7  apt: python3, git, caddy"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 git curl gnupg debian-keyring debian-archive-keyring apt-transport-https ca-certificates

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> 2/7  GitHub deploy-key"
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if [[ ! -f "$SSH_KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -C "aiboard-deploy@$(hostname)"
fi
ssh-keyscan -t ed25519,rsa github.com >> /root/.ssh/known_hosts 2>/dev/null || true
sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts
cat > /root/.ssh/config <<EOF
Host github.com
  IdentityFile $SSH_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chmod 600 /root/.ssh/config

echo "==> 3/7  /srv/aiboard"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Положить файлы туда из bundle (SRC_DIR), если каталог пустой и нет git-репо
if [[ ! -d "$APP_DIR/.git" ]]; then
  # Копируем содержимое bundle (исключая саму /srv/aiboard если bundle лежит ниже)
  if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    cp -a "$SRC_DIR/." "$APP_DIR/"
  fi
  git init -q -b main
  git -c user.email=root@localhost -c user.name=aiboard add -A
  git -c user.email=root@localhost -c user.name=aiboard commit -q -m "bootstrap" || true
fi

if [[ -n "$REPO_URL" ]]; then
  echo "==> attaching origin = $REPO_URL"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REPO_URL"
  else
    git remote add origin "$REPO_URL"
  fi
  if ! git fetch origin; then
    cat <<EOF >&2

================================================================
GitHub отказал в доступе. Добавьте этот публичный ключ в репозиторий:

  GitHub → ваш репо → Settings → Deploy keys → Add deploy key
  Title:               aiboard VDS
  Allow write access:  OFF (только чтение)
  Key:

$(cat "$SSH_KEY.pub")

После добавления ключа повторите запуск install.sh с тем же REPO_URL.
================================================================
EOF
    exit 1
  fi
  git reset --hard origin/main
fi

echo "==> 4/7  user aiproxy"
id -u aiproxy >/dev/null 2>&1 \
  || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin aiproxy

# Файлы /srv/aiboard оставляем под root:root (чтобы можно было редактировать руками
# без сюрпризов прав), aiproxy и caddy и так читают мир-доступ через 0644/0755.

echo "==> 5/7  systemd unit aiproxy"
install -m 0644 "$APP_DIR/deploy/aiproxy.service" /etc/systemd/system/aiproxy.service
systemctl daemon-reload
systemctl enable --now aiproxy
sleep 1
systemctl --no-pager --lines=5 status aiproxy || true

echo "==> 6/7  Caddyfile"
install -d -m 0755 /var/log/caddy
chown -R caddy:caddy /var/log/caddy
install -m 0644 "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
sleep 2
systemctl --no-pager --lines=5 status caddy || true

echo "==> 7/7  aiboard-sync.timer (git pull каждые 15 сек)"
install -m 0644 "$APP_DIR/deploy/aiboard-sync.service" /etc/systemd/system/aiboard-sync.service
install -m 0644 "$APP_DIR/deploy/aiboard-sync.timer"   /etc/systemd/system/aiboard-sync.timer
chmod +x "$APP_DIR/deploy/aiboard-sync.sh"
systemctl daemon-reload
systemctl enable --now aiboard-sync.timer

cat <<EOF

================================================================
Готово.

  Live site:    https://$DOMAIN
  Папка:        $APP_DIR
  Логи:
    journalctl -u aiproxy        -f
    journalctl -u caddy          -f
    journalctl -u aiboard-sync   -f

GitHub deploy-key (положите в Settings → Deploy keys выбранного репозитория,
write-access не нужен):

$(cat "$SSH_KEY.pub")

Когда репо будет создан и ключ добавлен — выполните на VDS:
    sudo REPO_URL=git@github.com:USER/aiboard.git bash $APP_DIR/deploy/install.sh

После этого правки в репо через мобильный Claude/GitHub автоматически
прилетают на $DOMAIN в течение ~15 секунд.
================================================================
EOF

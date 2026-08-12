#!/usr/bin/env bash
#
# One-time setup on a fresh Debian/Ubuntu VPS. Safe to re-run.
#
#   sudo bash deploy/install.sh
#
# Run this from the copied project directory, e.g. after
#   scp -r "Telegram copier" root@VPS:/tmp/copier-src
#   cd /tmp/copier-src && sudo bash deploy/install.sh

set -euo pipefail

APP_DIR=/opt/copier
APP_USER=copier
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync

echo "==> Creating service user '$APP_USER'"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copying application to $APP_DIR"
mkdir -p "$APP_DIR"
# Never overwrite live state: the database holds your API token and the
# session directory holds your Telegram login.
rsync -a --delete \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude 'data/' \
    --exclude 'session/' \
    "$SRC_DIR"/ "$APP_DIR"/

mkdir -p "$APP_DIR/data" "$APP_DIR/session"

echo "==> Building virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Fixing ownership and permissions"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data" "$APP_DIR/session"

echo "==> Installing systemd service"
cp "$APP_DIR/deploy/copier.service" /etc/systemd/system/copier.service
systemctl daemon-reload
systemctl enable copier
systemctl restart copier

sleep 3
systemctl --no-pager --lines=10 status copier || true

cat <<EOF

==> Done.

The dashboard is bound to 127.0.0.1 and is NOT reachable from the internet.
Open an SSH tunnel from your own machine:

    ssh -N -L 8501:127.0.0.1:8501 $(logname 2>/dev/null || echo root)@$(hostname -I 2>/dev/null | awk '{print $1}')

then browse to http://localhost:8501

Useful commands:
    sudo systemctl status copier      # is it running
    sudo systemctl restart copier     # after a code change
    sudo journalctl -u copier -f      # live logs
EOF

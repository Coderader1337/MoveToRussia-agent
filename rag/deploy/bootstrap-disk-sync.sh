#!/usr/bin/env bash
# One-time install of the daily Yandex Disk → Qdrant sync timer on prod VPS.
# Run as root on the VPS after deploy:
#   bash /opt/movetorussia/rag/deploy/bootstrap-disk-sync.sh
set -euo pipefail

APP_DIR="${1:-/opt/movetorussia/rag}"

cp "$APP_DIR/deploy/movetorussia-rag-disk-sync.service" /etc/systemd/system/
cp "$APP_DIR/deploy/movetorussia-rag-disk-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now movetorussia-rag-disk-sync.timer
systemctl list-timers movetorussia-rag-disk-sync.timer --no-pager
echo "Installed. Manual run: systemctl start movetorussia-rag-disk-sync.service"

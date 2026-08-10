#!/usr/bin/env bash
# Run on VPS after code sync. Args: <app_dir> <systemd_service_name>
set -euo pipefail

APP_DIR="${1:?app dir required}"
SERVICE="${2:?systemd service name required}"

cd "$APP_DIR"

if [[ ! -d venv ]]; then
  python3 -m venv venv
fi

venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE"
journalctl -u "$SERVICE" -n 8 --no-pager

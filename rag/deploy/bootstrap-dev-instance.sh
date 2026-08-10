#!/usr/bin/env bash
# One-time setup of the dev/test RAG bot instance on VPS.
# Usage (on VPS as root): bash bootstrap-dev-instance.sh <TELEGRAM_BOT_TOKEN>
set -euo pipefail

DEV_TOKEN="${1:?TELEGRAM_BOT_TOKEN for dev bot required}"
PROD_DIR="/opt/movetorussia/rag"
DEV_DIR="/opt/movetorussia/rag-dev"
SERVICE="movetorussia-rag-bot-dev"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$DEV_DIR"/{bot,data,prompt_data,mtr_rag,scripts,deploy}

if [[ -f "$PROD_DIR/requirements.txt" ]]; then
  cp "$PROD_DIR/requirements.txt" "$DEV_DIR/"
fi

if [[ -f "$PROD_DIR/.env" ]]; then
  grep -v '^TELEGRAM_BOT_TOKEN=' "$PROD_DIR/.env" > "$DEV_DIR/.env"
else
  touch "$DEV_DIR/.env"
fi

{
  echo "TELEGRAM_BOT_TOKEN=${DEV_TOKEN}"
  echo "MTR_STATS_CSV_PATH=${DEV_DIR}/data/usage_stats.csv"
  echo "MTR_COMMUNICATION_PRINCIPLES_PATH=${DEV_DIR}/prompt_data/communication_principles.txt"
  echo "MTR_TELEGRAM_WHITELIST_PATH=${DEV_DIR}/bot/allowed_users.json"
} >> "$DEV_DIR/.env"

if [[ -f "$PROD_DIR/bot/allowed_users.json" ]]; then
  cp "$PROD_DIR/bot/allowed_users.json" "$DEV_DIR/bot/"
fi

if [[ -f "$PROD_DIR/prompt_data/communication_principles.txt" ]]; then
  cp "$PROD_DIR/prompt_data/communication_principles.txt" "$DEV_DIR/prompt_data/"
fi

cp "$SCRIPT_DIR/movetorussia-rag-bot-dev.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"
echo "Dev instance prepared at ${DEV_DIR}. Run install-and-restart.sh after code sync."

#!/bin/bash
# Batch A/B: prod bot answers vs RAG on movetorussia_kb_exp (standard KB + Telegram).
# Same pipeline as the Telegram bot, but writes CSV instead of sending to chat.
set -euo pipefail
cd /opt/movetorussia/rag-dev
export QDRANT_URL=http://localhost:6335
export QDRANT_COLLECTION=movetorussia_kb_exp
exec ./venv/bin/python scripts/run_exp_batch.py \
  --input-csv /opt/movetorussia/rag/data/usage_stats.csv \
  --output-csv data/exp_comparison.csv \
  "$@"

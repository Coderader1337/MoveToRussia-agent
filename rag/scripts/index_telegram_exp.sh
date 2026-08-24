#!/bin/bash
set -euo pipefail
cd /opt/movetorussia/rag-dev
export QDRANT_URL=http://localhost:6335
export QDRANT_COLLECTION=movetorussia_kb_exp
exec ./venv/bin/python scripts/index_corpus.py \
  --corpus telegram_export_RAG/corpus_telegram.jsonl \
  --no-faq \
  --batch-size 1 \
  --sleep-between-batches 22 \
  --voyage-max-retries 6 \
  --voyage-base-delay 22

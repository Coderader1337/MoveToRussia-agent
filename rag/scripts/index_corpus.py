#!/usr/bin/env python3
"""Index mailbox_export_RAG/corpus.jsonl (+ FAQ catalog) into Qdrant via Voyage embeddings.

This is deliberately a standalone, reusable module (not wired into the bot)
so the same logic can later be dropped into an n8n "Execute Command"/HTTP
step for incremental ingestion of newly arrived emails.

Usage:
    python scripts/index_corpus.py --dry-run
    python scripts/index_corpus.py --recreate
    python scripts/index_corpus.py --batch-size 32 --no-faq
    python scripts/index_corpus.py --limit 20          # smoke test on a small sample
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtr_rag.config import settings  # noqa: E402
from mtr_rag.embeddings import VoyageEmbedder  # noqa: E402
from mtr_rag.loaders import load_all_chunks  # noqa: E402
from mtr_rag.qdrant_store import ensure_collection, get_client, upsert_chunks  # noqa: E402
from mtr_rag.schema import Chunk  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("index_corpus")

# Approximate Voyage AI pricing for voyage-4-large as of mid-2026 (USD per 1M tokens).
# Used only to print a rough dry-run cost estimate; not billed anywhere in code.
VOYAGE_PRICE_PER_1M_TOKENS = 0.18


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", type=Path, default=settings.corpus_path, help="Path to corpus.jsonl")
    p.add_argument("--faq-csv", type=Path, default=settings.faq_csv_path, help="Path to FAQ catalog CSV")
    p.add_argument("--no-faq", dest="include_faq", action="store_false", default=True,
                   help="Skip the FAQ catalog, index only mailbox corpus")
    p.add_argument("--recreate", action="store_true", help="Drop and recreate the Qdrant collection")
    p.add_argument("--batch-size", type=int, default=settings.voyage_batch_size,
                   help="Texts per Voyage embed call (default from VOYAGE_BATCH_SIZE)")
    p.add_argument("--dry-run", action="store_true",
                   help="Count tokens/estimate cost without calling Voyage or writing to Qdrant")
    p.add_argument("--limit", type=int, default=None, help="Index only the first N chunks (smoke testing)")
    p.add_argument(
        "--sleep-between-batches",
        type=float,
        default=2.0,
        help="Seconds to wait between batches (helps avoid Voyage rate limits)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    logger.info("Loading chunks from %s (faq=%s)", args.corpus, args.include_faq)
    chunks: list[Chunk] = load_all_chunks(args.corpus, args.faq_csv, include_faq=args.include_faq)
    if args.limit:
        chunks = chunks[: args.limit]
    if not chunks:
        logger.error("No chunks to index, aborting.")
        return 1

    by_source: dict[str, int] = {}
    for c in chunks:
        by_source[c.source] = by_source.get(c.source, 0) + 1
    logger.info("Loaded %d chunks: %s", len(chunks), by_source)

    embedder = VoyageEmbedder(batch_size=args.batch_size)
    texts = [c.embedding_text() for c in chunks]

    if args.dry_run:
        total_tokens = 0
        for i in range(0, len(texts), 128):
            total_tokens += embedder.count_tokens(texts[i : i + 128])
        est_cost = total_tokens / 1_000_000 * VOYAGE_PRICE_PER_1M_TOKENS
        logger.info(
            "[dry-run] chunks=%d tokens=%d est_cost≈$%.4f (@$%.2f/1M tok, model=%s)",
            len(chunks), total_tokens, est_cost, VOYAGE_PRICE_PER_1M_TOKENS, embedder.model,
        )
        return 0

    client = get_client()
    ensure_collection(client, vector_size=embedder.output_dimension, recreate=args.recreate)

    indexed, errors = 0, 0
    t0 = time.time()
    for start in range(0, len(chunks), args.batch_size):
        batch_chunks = chunks[start : start + args.batch_size]
        batch_texts = texts[start : start + args.batch_size]
        try:
            vectors = embedder.embed_documents(batch_texts)
            upsert_chunks(client, batch_chunks, vectors)
            indexed += len(batch_chunks)
        except Exception:
            errors += len(batch_chunks)
            logger.exception("Failed to index batch starting at %d", start)
        logger.info("Progress: %d/%d indexed, %d errors", indexed, len(chunks), errors)
        if args.sleep_between_batches > 0 and start + args.batch_size < len(chunks):
            time.sleep(args.sleep_between_batches)

    elapsed = time.time() - t0
    logger.info(
        "Done. indexed=%d errors=%d elapsed=%.1fs collection=%s",
        indexed, errors, elapsed, settings.qdrant_collection,
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

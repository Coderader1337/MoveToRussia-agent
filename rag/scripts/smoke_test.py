#!/usr/bin/env python3
"""End-to-end smoke test on a small sample (no real Qdrant server required).

Indexes ~15-20 chunks (mailbox + FAQ) into a local on-disk Qdrant instance
(qdrant-client embedded mode) and runs a couple of sample manager questions
through the full retrieval + DeepSeek generation chain.

Usage:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

LOCAL_DB_PATH = Path(__file__).resolve().parent.parent / "_smoke_qdrant_db"

# Must be set before importing mtr_rag.config, which reads env at import time.
os.environ["QDRANT_URL"] = f"path:{LOCAL_DB_PATH}"
os.environ["QDRANT_COLLECTION"] = "movetorussia_kb_smoketest"

from mtr_rag.chain import ask  # noqa: E402
from mtr_rag.config import settings  # noqa: E402
from mtr_rag.embeddings import FakeDeterministicEmbedder, VoyageEmbedder  # noqa: E402
from mtr_rag.loaders import load_all_chunks  # noqa: E402
from mtr_rag.qdrant_store import ensure_collection, get_client, upsert_chunks  # noqa: E402

SAMPLE_QUESTIONS = [
    "Клиент спрашивает про стоимость White Gloves пакета — что мы обычно отвечаем?",
    "Какие документы нужны клиенту для подачи на Shared Values Visa?",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--fake-embeddings",
        action="store_true",
        help=(
            "Use a deterministic hash-based embedder instead of Voyage AI. "
            "Only for environments where api.voyageai.com is unreachable; "
            "validates Qdrant storage/retrieval/DeepSeek wiring, but NOT "
            "real semantic search quality."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if LOCAL_DB_PATH.exists():
        shutil.rmtree(LOCAL_DB_PATH)

    print("=== 1. Loading a small sample of chunks ===")
    chunks = load_all_chunks(settings.corpus_path, settings.faq_csv_path, include_faq=True)
    sample = chunks[:12] + [c for c in chunks if c.source == "faq_catalog"][:8]
    print(f"Sample size: {len(sample)} (sources: "
          f"{ {c.source for c in sample} })")

    print("\n=== 2. Embedding + indexing into local on-disk Qdrant ===")
    embedder = FakeDeterministicEmbedder() if args.fake_embeddings else VoyageEmbedder()
    if args.fake_embeddings:
        print("!! Using FakeDeterministicEmbedder -- Voyage AI is bypassed. "
              "Retrieval quality below is NOT representative of the real system.")
    texts = [c.embedding_text() for c in sample]
    vectors = embedder.embed_documents(texts)
    client = get_client()
    ensure_collection(client, vector_size=embedder.output_dimension, recreate=True)
    upsert_chunks(client, sample, vectors)
    print(f"Indexed {len(sample)} chunks into collection '{settings.qdrant_collection}'.")
    client.close()  # release the on-disk lock so the retriever can open its own client

    print("\n=== 3. Retrieval + DeepSeek generation ===")
    for q in SAMPLE_QUESTIONS:
        print(f"\n--- Question: {q}")
        result = ask(q, top_k=5, embedder=embedder if args.fake_embeddings else None)
        print(f"Answer:\n{result.answer}\n")
        print("Sources:")
        for s in result.sources:
            print(f"  - {s}")

    print("\nSmoke test finished OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Sync Yandex Disk txt files and incrementally update only yandex_disk chunks in Qdrant.

Does NOT touch mailbox_thread or faq_catalog points. Run daily via systemd timer
on the prod VPS instance (/opt/movetorussia/rag).

Usage:
    python scripts/update_yandex_disk_corpus.py --dry-run
    python scripts/update_yandex_disk_corpus.py
    python scripts/update_yandex_disk_corpus.py --skip-sync
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
from mtr_rag.qdrant_store import (  # noqa: E402
    delete_chunk_ids,
    ensure_collection,
    get_client,
    reconcile_source,
    upsert_chunks,
)
from mtr_rag.yandex_disk_sync import (  # noqa: E402
    DISK_SOURCE,
    SyncDiff,
    build_corpus_from_local,
    chunks_for_entries,
    compute_diff,
    load_manifest,
    sync_from_yandex_disk,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("update_yandex_disk_corpus")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Show planned changes without writing to Qdrant")
    p.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip Yandex Disk download; rebuild corpus from local files/ mirror only",
    )
    p.add_argument(
        "--sleep-between-batches",
        type=float,
        default=2.0,
        help="Seconds to wait between Voyage embed batches",
    )
    return p.parse_args()


def _resolve_diff(args: argparse.Namespace) -> tuple[SyncDiff, dict[str, ManifestEntry]]:
    persist = not args.dry_run
    old_manifest = load_manifest()
    if args.skip_sync:
        new_entries = build_corpus_from_local(persist=persist)
        return compute_diff(old_manifest, new_entries), new_entries
    return sync_from_yandex_disk(persist=persist)


def main() -> int:
    args = parse_args()
    diff, current_entries = _resolve_diff(args)
    expected_ids = set(current_entries)

    logger.info(
        "Diff: added=%d changed=%d removed=%d current_total=%d",
        len(diff.added), len(diff.changed), len(diff.removed), len(expected_ids),
    )

    to_upsert_entries = diff.to_upsert
    to_delete_ids = [e.chunk_id for e in diff.removed]

    if args.dry_run:
        logger.info(
            "[dry-run] would upsert %d chunk(s), delete %d, reconcile to %d total",
            len(to_upsert_entries), len(to_delete_ids), len(expected_ids),
        )
        for entry in to_upsert_entries:
            logger.info("  upsert: %s (%s)", entry.chunk_id, entry.file_path)
        for chunk_id in to_delete_ids:
            logger.info("  delete: %s", chunk_id)
        return 0

    chunks_to_index = chunks_for_entries(to_upsert_entries)

    embedder = VoyageEmbedder(batch_size=settings.voyage_batch_size)
    client = get_client()
    ensure_collection(client, vector_size=embedder.output_dimension, recreate=False)

    indexed, errors = 0, 0
    if chunks_to_index:
        texts = [c.embedding_text() for c in chunks_to_index]
        batch_size = settings.voyage_batch_size
        for start in range(0, len(chunks_to_index), batch_size):
            batch_chunks = chunks_to_index[start : start + batch_size]
            batch_texts = texts[start : start + batch_size]
            try:
                vectors = embedder.embed_documents(batch_texts)
                upsert_chunks(client, batch_chunks, vectors)
                indexed += len(batch_chunks)
            except Exception:
                errors += len(batch_chunks)
                logger.exception("Failed to index batch starting at %d", start)
            if args.sleep_between_batches > 0 and start + batch_size < len(chunks_to_index):
                time.sleep(args.sleep_between_batches)

    if to_delete_ids:
        delete_chunk_ids(client, to_delete_ids)
        logger.info("Deleted %d removed chunk(s) from Qdrant", len(to_delete_ids))

    orphans = reconcile_source(client, source=DISK_SOURCE, expected_chunk_ids=expected_ids)
    if orphans:
        logger.info("Reconcile removed %d orphan chunk(s): %s", len(orphans), orphans)

    logger.info(
        "Done. upserted=%d deleted=%d orphans=%d errors=%d collection=%s",
        indexed, len(to_delete_ids), len(orphans), errors, settings.qdrant_collection,
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Upsert Qdrant points that exist in source but not in target.

Typical use: promote experiment data (e.g. telegram_chat vectors) into prod
without touching points that already exist in the target collection.

Usage:
    python scripts/sync_missing_qdrant_points.py \\
        --source-url http://localhost:6335 --source-collection movetorussia_kb_exp \\
        --target-url http://localhost:6333 --target-collection movetorussia_kb

    python scripts/sync_missing_qdrant_points.py ... --source-filter telegram_chat
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sync_missing_qdrant")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-collection", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--target-collection", required=True)
    p.add_argument("--source-filter", default=None, help="Only copy points with this payload source value")
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--sleep-ms", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def make_client(url: str) -> QdrantClient:
    return QdrantClient(url=url, check_compatibility=False)


def assert_healthy(client: QdrantClient, collection: str) -> int:
    info = client.get_collection(collection)
    if info.status != qm.CollectionStatus.GREEN:
        raise RuntimeError(f"Collection {collection!r} status is {info.status!r}, aborting")
    return info.points_count or 0


def load_target_ids(client: QdrantClient, collection: str) -> set[str]:
    ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.update(str(point.id) for point in points)
        if offset is None:
            break
    return ids


def sync_missing(
    source: QdrantClient,
    target: QdrantClient,
    *,
    source_name: str,
    target_name: str,
    target_ids: set[str],
    source_filter: str | None,
    batch_size: int,
    sleep_ms: int,
    dry_run: bool,
) -> tuple[int, int]:
    scroll_filter = None
    if source_filter:
        scroll_filter = qm.Filter(
            must=[qm.FieldCondition(key="source", match=qm.MatchValue(value=source_filter))]
        )

    offset = None
    scanned = 0
    copied = 0
    batch_no = 0
    pending: list[qm.PointStruct] = []

    while True:
        points, offset = source.scroll(
            collection_name=source_name,
            scroll_filter=scroll_filter,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break

        for point in points:
            scanned += 1
            if str(point.id) in target_ids:
                continue
            pending.append(qm.PointStruct(id=point.id, vector=point.vector, payload=point.payload))

        while len(pending) >= batch_size:
            batch_no += 1
            batch = pending[:batch_size]
            pending = pending[batch_size:]
            if not dry_run:
                target.upsert(collection_name=target_name, points=batch)
                target_ids.update(str(point.id) for point in batch)
            copied += len(batch)
            logger.info("Batch %d: upserted %d missing points (scanned=%d)", batch_no, copied, scanned)

        if offset is None:
            break
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000)

    if pending:
        batch_no += 1
        if not dry_run:
            target.upsert(collection_name=target_name, points=pending)
            target_ids.update(str(point.id) for point in pending)
        copied += len(pending)
        logger.info("Batch %d: upserted %d missing points (scanned=%d)", batch_no, copied, scanned)

    return scanned, copied


def main() -> int:
    args = parse_args()
    source = make_client(args.source_url)
    target = make_client(args.target_url)

    for client, name, url in (
        (source, args.source_collection, args.source_url),
        (target, args.target_collection, args.target_url),
    ):
        if not client.collection_exists(name):
            logger.error("Collection %s not found at %s", name, url)
            return 1

    source_before = assert_healthy(source, args.source_collection)
    target_before = assert_healthy(target, args.target_collection)
    logger.info("Source %s: %d points", args.source_collection, source_before)
    logger.info("Target %s: %d points (before)", args.target_collection, target_before)

    target_ids = load_target_ids(target, args.target_collection)
    logger.info("Loaded %d target point ids", len(target_ids))

    scanned, copied = sync_missing(
        source,
        target,
        source_name=args.source_collection,
        target_name=args.target_collection,
        target_ids=target_ids,
        source_filter=args.source_filter,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
        dry_run=args.dry_run,
    )

    target_after = assert_healthy(target, args.target_collection)
    summary = {
        "source": args.source_collection,
        "target": args.target_collection,
        "source_filter": args.source_filter,
        "dry_run": args.dry_run,
        "source_points": source_before,
        "target_points_before": target_before,
        "target_points_after": target_after,
        "scanned": scanned,
        "copied": copied,
        "ok": not args.dry_run and target_after == source_before,
    }
    logger.info("Done: %s", json.dumps(summary))
    if args.dry_run:
        return 0
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Copy a Qdrant collection via read-only scroll on source + upsert on target.

Designed for prod-safe cloning: the source collection is never written to
(no upsert/delete/recreate/snapshot). Aborts if source health degrades.

Usage:
    python scripts/clone_qdrant_collection.py \\
        --source-url http://localhost:6333 --source-collection movetorussia_kb \\
        --target-url http://localhost:6335 --target-collection movetorussia_kb_exp

    python scripts/clone_qdrant_collection.py ... --recreate-target
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
logger = logging.getLogger("clone_qdrant")

PAYLOAD_INDEXES: tuple[tuple[str, qm.PayloadSchemaType], ...] = (
    ("source", qm.PayloadSchemaType.KEYWORD),
    ("thread_id", qm.PayloadSchemaType.KEYWORD),
    ("client_email", qm.PayloadSchemaType.KEYWORD),
    ("manager_emails", qm.PayloadSchemaType.KEYWORD),
    ("low_signal", qm.PayloadSchemaType.BOOL),
    ("date_start", qm.PayloadSchemaType.DATETIME),
    ("language", qm.PayloadSchemaType.KEYWORD),
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-url", default="http://localhost:6333")
    p.add_argument("--source-collection", required=True)
    p.add_argument("--target-url", default="http://localhost:6335")
    p.add_argument("--target-collection", required=True)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--sleep-ms", type=int, default=150, help="Pause between batches")
    p.add_argument("--recreate-target", action="store_true", help="Drop target collection before copy")
    return p.parse_args()


def make_client(url: str) -> QdrantClient:
    return QdrantClient(url=url, check_compatibility=False)


def assert_source_healthy(client: QdrantClient, collection: str) -> int:
    info = client.get_collection(collection)
    if info.status != qm.CollectionStatus.GREEN:
        raise RuntimeError(f"Source collection {collection!r} status is {info.status!r}, aborting")
    return info.points_count or 0


def ensure_target_collection(
    client: QdrantClient,
    name: str,
    *,
    vector_size: int,
    distance: qm.Distance,
    recreate: bool,
) -> None:
    if client.collection_exists(name):
        if recreate:
            logger.info("Deleting existing target collection %s", name)
            client.delete_collection(name)
        else:
            raise RuntimeError(
                f"Target collection {name!r} already exists; pass --recreate-target to replace it"
            )
    logger.info("Creating target collection %s (size=%s, distance=%s)", name, vector_size, distance)
    client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=vector_size, distance=distance),
    )
    for field_name, schema_type in PAYLOAD_INDEXES:
        client.create_payload_index(name, field_name=field_name, field_schema=schema_type)


def copy_collection(
    source: QdrantClient,
    target: QdrantClient,
    *,
    source_name: str,
    target_name: str,
    batch_size: int,
    sleep_ms: int,
) -> int:
    src_info = source.get_collection(source_name)
    vector_size = src_info.config.params.vectors.size
    distance = src_info.config.params.vectors.distance
    expected = assert_source_healthy(source, source_name)

    offset = None
    copied = 0
    batch_no = 0

    while True:
        assert_source_healthy(source, source_name)

        points, offset = source.scroll(
            collection_name=source_name,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break

        batch_no += 1
        target.upsert(
            collection_name=target_name,
            points=[
                qm.PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                for point in points
            ],
        )
        copied += len(points)
        logger.info("Batch %d: copied %d / ~%d points", batch_no, copied, expected)

        if offset is None:
            break
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000)

    return copied


def main() -> int:
    args = parse_args()
    source = make_client(args.source_url)
    target = make_client(args.target_url)

    if not source.collection_exists(args.source_collection):
        logger.error("Source collection %s not found at %s", args.source_collection, args.source_url)
        return 1

    src_info = source.get_collection(args.source_collection)
    expected = assert_source_healthy(source, args.source_collection)
    logger.info("Source %s: %d points, status=green", args.source_collection, expected)

    vector_size = src_info.config.params.vectors.size
    distance = src_info.config.params.vectors.distance
    ensure_target_collection(
        target,
        args.target_collection,
        vector_size=vector_size,
        distance=distance,
        recreate=args.recreate_target,
    )

    copied = copy_collection(
        source,
        target,
        source_name=args.source_collection,
        target_name=args.target_collection,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
    )

    final_source = assert_source_healthy(source, args.source_collection)
    final_target = target.get_collection(args.target_collection).points_count or 0

    summary = {
        "source": args.source_collection,
        "target": args.target_collection,
        "source_points_before": expected,
        "source_points_after": final_source,
        "target_points": final_target,
        "copied_points": copied,
        "ok": final_source == expected and final_target == expected,
    }
    logger.info("Done: %s", json.dumps(summary))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

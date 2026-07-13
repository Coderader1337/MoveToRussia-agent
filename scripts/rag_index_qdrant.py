"""
Индексация mailbox_export_clean (+ FAQ) в Qdrant для RAG.

Вход:  mailbox_export_clean/threads/*.txt
       knowledge_base/v4/client_faq_review.csv  (--include-faq)
Выход: коллекция Qdrant movetorussia_kb

Требует запущенные rag/docker-compose.yml (Qdrant + Voyage proxy embedder).

.env:
  QDRANT_URL=http://localhost:6333
  EMBEDDER_URL=http://localhost:8081
  RAG_COLLECTION=movetorussia_kb

Примеры:
  python scripts/rag_index_qdrant.py --dry-run
  python scripts/rag_index_qdrant.py
  python scripts/rag_index_qdrant.py --include-faq --recreate
  python scripts/rag_index_qdrant.py --file path/to/upload.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from rag_chunk import (  # noqa: E402
    RagChunk,
    iter_faq_chunks,
    iter_message_chunks_from_clean_threads,
    split_upload_text,
)

DEFAULT_COLLECTION = "movetorussia_kb"
BATCH_SIZE = 32


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def http_json(method: str, url: str, payload: dict | None = None, timeout: int = 120) -> Any:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e


def get_vector_size(embedder_url: str) -> int:
    info = http_json("GET", f"{embedder_url.rstrip('/')}/info")
    return int(info["dimensions"])


def ensure_collection(qdrant_url: str, collection: str, vector_size: int, recreate: bool) -> None:
    base = qdrant_url.rstrip("/")
    if recreate:
        try:
            http_json("DELETE", f"{base}/collections/{collection}")
        except RuntimeError:
            pass

    try:
        http_json("GET", f"{base}/collections/{collection}")
        print(f"  Коллекция {collection} уже существует")
        return
    except RuntimeError:
        pass

    payload = {
        "vectors": {"size": vector_size, "distance": "Cosine"},
        "optimizers_config": {"default_segment_number": 2},
    }
    http_json("PUT", f"{base}/collections/{collection}", payload)
    print(f"  Создана коллекция {collection} (dim={vector_size})")


def embed_texts(
    embedder_url: str,
    texts: list[str],
    *,
    input_type: str = "document",
) -> list[list[float]]:
    payload = {"texts": texts, "input_type": input_type}
    resp = http_json("POST", f"{embedder_url.rstrip('/')}/embed", payload, timeout=300)
    return resp["embeddings"]


def upsert_batch(
    qdrant_url: str,
    collection: str,
    chunks: list[RagChunk],
    vectors: list[list[float]],
) -> None:
    points = []
    for chunk, vector in zip(chunks, vectors):
        points.append(
            {
                "id": chunk.chunk_id,
                "vector": vector,
                "payload": {
                    "content": chunk.content,
                    **chunk.metadata,
                },
            }
        )
    http_json(
        "PUT",
        f"{qdrant_url.rstrip('/')}/collections/{collection}/points?wait=true",
        {"points": points},
    )


def collect_chunks(include_faq: bool, upload_file: Path | None) -> list[RagChunk]:
    chunks: list[RagChunk] = []
    seen: set[str] = set()

    def add(chunk: RagChunk) -> None:
        if chunk.chunk_id in seen:
            return
        seen.add(chunk.chunk_id)
        chunks.append(chunk)

    print("  Чтение mailbox_export_clean/threads…")
    for chunk in iter_message_chunks_from_clean_threads():
        add(chunk)

    if include_faq:
        print("  Чтение FAQ catalog…")
        for chunk in iter_faq_chunks():
            add(chunk)

    if upload_file:
        text = upload_file.read_text(encoding="utf-8")
        for chunk in split_upload_text(text, source_name=upload_file.name):
            add(chunk)

    return chunks


def index_chunks(
    chunks: list[RagChunk],
    *,
    qdrant_url: str,
    embedder_url: str,
    collection: str,
    dry_run: bool,
) -> dict[str, int]:
    stats = {"chunks": len(chunks), "indexed": 0, "batches": 0}
    if dry_run or not chunks:
        return stats

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c.content for c in batch]
        vectors = embed_texts(embedder_url, texts, input_type="document")
        upsert_batch(qdrant_url, collection, batch, vectors)
        stats["indexed"] += len(batch)
        stats["batches"] += 1
        print(f"    indexed {stats['indexed']}/{len(chunks)}", end="\r")
        time.sleep(0.05)

    print()
    return stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Только подсчёт чанков")
    p.add_argument("--recreate", action="store_true", help="Пересоздать коллекцию Qdrant")
    p.add_argument("--include-faq", action="store_true", help="Добавить FAQ из knowledge_base/v4")
    p.add_argument("--file", type=Path, help="Дополнительный .txt для индексации")
    p.add_argument("--collection", default=_env("RAG_COLLECTION", DEFAULT_COLLECTION))
    p.add_argument("--qdrant-url", default=_env("QDRANT_URL", "http://localhost:6333"))
    p.add_argument("--embedder-url", default=_env("EMBEDDER_URL", "http://localhost:8081"))
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "rag" / ".env")
    args = parse_args()

    print("=== MoveToRussia RAG indexer ===")
    print(f"  Qdrant:    {args.qdrant_url}")
    print(f"  Embedder:  {args.embedder_url}")
    print(f"  Collection:{args.collection}")

    chunks = collect_chunks(args.include_faq, args.file)
    by_type: dict[str, int] = {}
    for c in chunks:
        key = c.metadata.get("source_type", "unknown")
        by_type[key] = by_type.get(key, 0) + 1

    print(f"  Всего чанков: {len(chunks)}")
    for k, v in sorted(by_type.items()):
        print(f"    {k}: {v}")

    if args.dry_run:
        print("  dry-run — индексация пропущена")
        return 0

    vector_size = get_vector_size(args.embedder_url)
    ensure_collection(args.qdrant_url, args.collection, vector_size, args.recreate)
    stats = index_chunks(
        chunks,
        qdrant_url=args.qdrant_url,
        embedder_url=args.embedder_url,
        collection=args.collection,
        dry_run=False,
    )
    print(f"  Готово: {stats['indexed']} чанков в {stats['batches']} батчах")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

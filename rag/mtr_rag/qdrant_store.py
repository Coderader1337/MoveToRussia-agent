"""Thin Qdrant wrapper: collection management + point upsert/search.

Kept independent from LangChain on purpose so `scripts/index_corpus.py` (and
the future n8n ingestion job) can reuse it without pulling in retriever/LLM
dependencies.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .config import settings
from .schema import Chunk

# Fixed namespace so the same chunk id always maps to the same Qdrant point id
# across re-indexing runs (lets upserts overwrite rather than duplicate).
_POINT_NAMESPACE = uuid.UUID("6f6a5e2e-6e2a-4d1b-9a8b-0f6c9a6b1a10")


def chunk_id_to_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


def get_client() -> QdrantClient:
    """Build a Qdrant client from QDRANT_URL.

    Normally QDRANT_URL is an http(s) URL to the self-hosted Qdrant server.
    For local development/smoke-testing without a running server, set
    QDRANT_URL=path:./some/local/dir to use qdrant-client's embedded on-disk
    mode (same client API, no server process required).
    """
    if settings.qdrant_url.startswith("path:"):
        return QdrantClient(path=settings.qdrant_url[len("path:"):])
    kwargs: dict[str, Any] = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def ensure_collection(client: QdrantClient, *, vector_size: int, recreate: bool = False) -> None:
    name = settings.qdrant_collection
    exists = client.collection_exists(name)
    if exists and recreate:
        client.delete_collection(name)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
        )
        # Payload indexes enable efficient filtering by source/date/manager.
        for field_name, schema_type in (
            ("source", qm.PayloadSchemaType.KEYWORD),
            ("thread_id", qm.PayloadSchemaType.KEYWORD),
            ("client_email", qm.PayloadSchemaType.KEYWORD),
            ("manager_emails", qm.PayloadSchemaType.KEYWORD),
            ("low_signal", qm.PayloadSchemaType.BOOL),
            ("date_start", qm.PayloadSchemaType.DATETIME),
            ("language", qm.PayloadSchemaType.KEYWORD),
        ):
            client.create_payload_index(name, field_name=field_name, field_schema=schema_type)


def delete_chunk_ids(client: QdrantClient, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    point_ids = [chunk_id_to_point_id(cid) for cid in chunk_ids]
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=qm.PointIdsList(points=point_ids),
    )


def list_chunk_ids_by_source(client: QdrantClient, source: str) -> list[str]:
    """Scroll all point ids for a given source value (for reconcile)."""
    ids: list[str] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="source", match=qm.MatchValue(value=source))]
            ),
            limit=256,
            offset=offset,
            with_payload=["id"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            chunk_id = payload.get("id")
            if chunk_id:
                ids.append(str(chunk_id))
        if offset is None:
            break
    return ids


def reconcile_source(
    client: QdrantClient,
    *,
    source: str,
    expected_chunk_ids: set[str],
) -> list[str]:
    """Delete Qdrant points for `source` that are not in expected_chunk_ids."""
    existing = set(list_chunk_ids_by_source(client, source))
    orphan_ids = sorted(existing - expected_chunk_ids)
    if orphan_ids:
        delete_chunk_ids(client, orphan_ids)
    return orphan_ids


def upsert_chunks(client: QdrantClient, chunks: list[Chunk], vectors: list[list[float]]) -> None:
    points = [
        qm.PointStruct(
            id=chunk_id_to_point_id(chunk.id),
            vector=vector,
            payload=chunk.to_payload(),
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    client.upsert(collection_name=settings.qdrant_collection, points=points)


def build_filter(
    *,
    source: str | None = None,
    exclude_low_signal: bool = False,
    manager_email: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> qm.Filter | None:
    must: list[qm.Condition] = []
    must_not: list[qm.Condition] = []

    if source:
        must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=source)))
    if manager_email:
        must.append(qm.FieldCondition(key="manager_emails", match=qm.MatchValue(value=manager_email)))
    if exclude_low_signal:
        must_not.append(qm.FieldCondition(key="low_signal", match=qm.MatchValue(value=True)))
    if date_from or date_to:
        range_kwargs: dict[str, Any] = {}
        if date_from:
            range_kwargs["gte"] = date_from
        if date_to:
            range_kwargs["lte"] = date_to
        must.append(qm.FieldCondition(key="date_start", range=qm.DatetimeRange(**range_kwargs)))

    if not must and not must_not:
        return None
    return qm.Filter(must=must or None, must_not=must_not or None)


def search(
    client: QdrantClient,
    query_vector: list[float],
    *,
    top_k: int,
    query_filter: qm.Filter | None = None,
) -> list[qm.ScoredPoint]:
    result = client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_vector,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    return result.points

"""Custom LangChain retriever backed by Qdrant + Voyage query embeddings.

A custom `BaseRetriever` (rather than a generic LangChain VectorStore wrapper)
was chosen so retrieval-time payload filters (source / manager / date range /
low_signal) map directly onto our own Qdrant payload schema without needing
an extra translation layer.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from langchain_core.embeddings import Embeddings

from .config import settings
from .embeddings import VoyageEmbedder
from .qdrant_store import build_filter, get_client, search
from .schema import Chunk
from .yandex_disk_sync import DISK_PRIORITY, DISK_SOURCE


def chunk_to_document(chunk: Chunk, score: float) -> Document:
    priority = chunk.extra.get("priority")
    if priority is None and chunk.source == DISK_SOURCE:
        priority = DISK_PRIORITY
    file_path = chunk.extra.get("file_path")
    return Document(
        page_content=chunk.text,
        metadata={
            "score": score,
            "thread_id": chunk.thread_id,
            "subject": chunk.subject,
            "source": chunk.source,
            "file_path": file_path,
            "priority": priority,
            "client_email": chunk.client_email,
            "manager_emails": chunk.manager_emails,
            "date_start": chunk.date_start,
            "date_end": chunk.date_end,
            "language": chunk.language,
            "low_signal": chunk.low_signal,
        },
    )


def _doc_key(doc: Document) -> str:
    meta = doc.metadata
    return f"{meta.get('source')}:{meta.get('thread_id')}:{hash(doc.page_content)}"


def merge_disk_priority(
    general: list[Document],
    disk: list[Document],
    *,
    top_k: int,
    disk_reserve_slots: int,
    disk_min_score: float,
) -> list[Document]:
    """Reserve slots for relevant official Yandex Disk files, fill rest from general search."""
    reserved: list[Document] = []
    for doc in disk:
        if len(reserved) >= disk_reserve_slots:
            break
        if (doc.metadata.get("score") or 0) < disk_min_score:
            continue
        reserved.append(doc)

    seen = {_doc_key(doc) for doc in reserved}
    merged = list(reserved)
    for doc in general:
        if len(merged) >= top_k:
            break
        key = _doc_key(doc)
        if key in seen:
            continue
        merged.append(doc)
        seen.add(key)
    return merged


class MtrKnowledgeBaseRetriever(BaseRetriever):
    """Retrieves manager mailbox precedents + FAQ entries relevant to a question."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    embedder: Embeddings | None = None
    top_k: int = settings.retrieval_top_k
    source: str | None = None
    exclude_low_signal: bool = False
    manager_email: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("embedder", VoyageEmbedder())
        super().__init__(**kwargs)
        self._client = get_client()

    def _points_to_documents(self, points) -> list[Document]:
        documents = []
        for point in points:
            chunk = Chunk.from_payload(point.payload or {})
            documents.append(chunk_to_document(chunk, point.score))
        return documents

    def _search_points(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        source: str | None = None,
    ):
        query_filter = build_filter(
            source=source if source is not None else self.source,
            exclude_low_signal=self.exclude_low_signal,
            manager_email=self.manager_email,
        )
        return search(
            self._client,
            query_vector,
            top_k=top_k,
            query_filter=query_filter,
        )

    def search_by_vector(self, query_vector: list[float], *, top_k: int | None = None) -> list[Document]:
        effective_k = top_k or self.top_k
        general_points = self._search_points(query_vector, top_k=effective_k)
        general = self._points_to_documents(general_points)

        # When searching all sources, reserve slots for relevant official disk files.
        if self.source is not None:
            return general

        disk_points = self._search_points(
            query_vector,
            top_k=settings.disk_reserve_slots,
            source=DISK_SOURCE,
        )
        disk_docs = self._points_to_documents(disk_points)
        return merge_disk_priority(
            general,
            disk_docs,
            top_k=effective_k,
            disk_reserve_slots=settings.disk_reserve_slots,
            disk_min_score=settings.disk_min_score,
        )

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        query_vector = self.embedder.embed_query(query)
        return self.search_by_vector(query_vector)

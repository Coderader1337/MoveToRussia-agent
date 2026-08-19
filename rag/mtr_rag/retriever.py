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
    return Document(
        page_content=chunk.text,
        metadata={
            "score": score,
            "thread_id": chunk.thread_id,
            "subject": chunk.subject,
            "source": chunk.source,
            "file_path": chunk.extra.get("file_path"),
            "priority": priority,
            "client_email": chunk.client_email,
            "manager_emails": chunk.manager_emails,
            "date_start": chunk.date_start,
            "date_end": chunk.date_end,
            "language": chunk.language,
            "low_signal": chunk.low_signal,
        },
    )


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

    def search_by_vector(self, query_vector: list[float], *, top_k: int | None = None) -> list[Document]:
        query_filter = build_filter(
            source=self.source,
            exclude_low_signal=self.exclude_low_signal,
            manager_email=self.manager_email,
        )
        points = search(
            self._client,
            query_vector,
            top_k=top_k or self.top_k,
            query_filter=query_filter,
        )
        documents = []
        for point in points:
            chunk = Chunk.from_payload(point.payload or {})
            documents.append(chunk_to_document(chunk, point.score))
        return documents

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        query_vector = self.embedder.embed_query(query)
        return self.search_by_vector(query_vector)

"""Voyage AI embedding wrapper used by both the indexer and the retriever.

Implements the LangChain `Embeddings` interface (embed_documents / embed_query)
so it can be dropped into any LangChain retriever/vectorstore, while giving us
full control over batching, input_type switching and retry/backoff -- which
the stock `langchain-voyageai` integration does not expose cleanly.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Sequence

import voyageai
from langchain_core.embeddings import Embeddings
from voyageai.error import RateLimitError, ServerError, ServiceUnavailableError

from .config import settings

logger = logging.getLogger(__name__)

RETRYABLE_EXCEPTIONS = (RateLimitError, ServerError, ServiceUnavailableError)


class VoyageEmbedder(Embeddings):
    """Voyage AI embeddings with exponential backoff on 429/500/502/503."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        output_dimension: int | None = None,
        batch_size: int | None = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        self.model = model or settings.voyage_model
        self.output_dimension = output_dimension or settings.voyage_output_dimension
        self.batch_size = batch_size or settings.voyage_batch_size
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._client = voyageai.Client(api_key=api_key or settings.voyage_api_key)

    def _embed_with_retry(self, texts: list[str], input_type: str) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                result = self._client.embed(
                    texts,
                    model=self.model,
                    input_type=input_type,
                    output_dimension=self.output_dimension,
                )
                return result.embeddings
            except RETRYABLE_EXCEPTIONS as exc:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error("Voyage embed failed after %d retries: %s", attempt - 1, exc)
                    raise
                delay = self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "Voyage embed error (%s), retry %d/%d in %.1fs",
                    type(exc).__name__, attempt, self.max_retries, delay,
                )
                time.sleep(delay)

    def _embed_batched(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        texts = list(texts)
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            out.extend(self._embed_with_retry(batch, input_type))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_batched(texts, input_type="document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed_with_retry([text], input_type="query")[0]

    def count_tokens(self, texts: Sequence[str]) -> int:
        try:
            return int(self._client.count_tokens(list(texts), model=self.model))
        except Exception:
            # Fallback rough estimate: ~4 chars/token.
            return sum(len(t) for t in texts) // 4


class FakeDeterministicEmbedder(Embeddings):
    """Hash-based deterministic embedder, for offline testing only.

    Used by scripts/smoke_test.py --fake-embeddings when the Voyage API is
    unreachable (e.g. blocked network), so the rest of the pipeline (Qdrant
    storage, retrieval, DeepSeek generation) can still be exercised end to
    end. Never use this for real indexing -- vectors carry no real semantics.
    """

    def __init__(self, dim: int = 256) -> None:
        self.output_dimension = dim
        self.model = "fake-deterministic"

    def _vector(self, text: str) -> list[float]:
        import hashlib

        vec = [0.0] * self.output_dimension
        words = text.lower().split()
        for w in words:
            h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
            vec[h % self.output_dimension] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def count_tokens(self, texts: Sequence[str]) -> int:
        return sum(len(t) for t in texts) // 4

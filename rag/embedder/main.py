"""OpenAI-compatible embedding API for n8n RAG (BGE-M3 via fastembed)."""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("embedder")

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

app = FastAPI(title="MoveToRussia Embedder", version="1.0.0")
_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        logger.info("Loading embedding model: %s", MODEL_NAME)
        _model = TextEmbedding(model_name=MODEL_NAME)
        logger.info("Model loaded")
    return _model


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    encoding_format: str | None = "float"


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict[str, int] = Field(default_factory=lambda: {"prompt_tokens": 0, "total_tokens": 0})


class EmbedBatchRequest(BaseModel):
    texts: list[str]


class EmbedBatchResponse(BaseModel):
    model: str
    dimensions: int
    embeddings: list[list[float]]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}],
    }


def _embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_model()
    vectors = list(model.embed(texts))
    return [v.tolist() for v in vectors]


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def openai_embeddings(body: EmbeddingRequest) -> EmbeddingResponse:
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts:
        raise HTTPException(status_code=400, detail="input is empty")
    if any(not t.strip() for t in texts):
        raise HTTPException(status_code=400, detail="empty text in input")

    started = time.perf_counter()
    vectors = _embed_texts(texts)
    elapsed = time.perf_counter() - started
    logger.info("Embedded %d texts in %.2fs", len(texts), elapsed)

    return EmbeddingResponse(
        data=[
            EmbeddingData(index=i, embedding=vec)
            for i, vec in enumerate(vectors)
        ],
        model=body.model or MODEL_NAME,
        usage={"prompt_tokens": sum(len(t.split()) for t in texts), "total_tokens": 0},
    )


@app.post("/embed", response_model=EmbedBatchResponse)
def embed_batch(body: EmbedBatchRequest) -> EmbedBatchResponse:
    if not body.texts:
        raise HTTPException(status_code=400, detail="texts is empty")
    vectors = _embed_texts(body.texts)
    dims = len(vectors[0]) if vectors else 0
    return EmbedBatchResponse(model=MODEL_NAME, dimensions=dims, embeddings=vectors)


@app.get("/info")
def info() -> dict[str, Any]:
    model = get_model()
    sample = list(model.embed(["query: test"]))[0]
    return {
        "model": MODEL_NAME,
        "dimensions": len(sample),
        "idempotency": hashlib.sha256(MODEL_NAME.encode()).hexdigest()[:12],
        "request_id": str(uuid.uuid4()),
    }

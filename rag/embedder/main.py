"""OpenAI-compatible embedding API for n8n RAG (Voyage AI proxy)."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("embedder")

MODEL_NAME = os.getenv("VOYAGE_MODEL", "voyage-4-large")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "").strip()
VOYAGE_BASE_URL = os.getenv("VOYAGE_BASE_URL", "https://api.voyageai.com").rstrip("/")
VOYAGE_OUTPUT_DIMENSION = int(os.getenv("VOYAGE_OUTPUT_DIMENSION", "2048"))
VOYAGE_OUTPUT_DTYPE = os.getenv("VOYAGE_OUTPUT_DTYPE", "float")
VOYAGE_TIMEOUT = int(os.getenv("VOYAGE_TIMEOUT", "120"))

app = FastAPI(title="MoveToRussia Embedder", version="1.0.0")


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    input_type: str | None = None
    output_dimension: int | None = None
    output_dtype: str | None = None
    truncation: bool | None = True
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
    input_type: str = "document"
    output_dimension: int | None = None
    output_dtype: str | None = None
    truncation: bool | None = True


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


def _embed_texts(
    texts: list[str],
    *,
    input_type: str | None = None,
    output_dimension: int | None = None,
    output_dtype: str | None = None,
    truncation: bool | None = True,
) -> list[list[float]]:
    if not texts:
        return []
    try:
        if not VOYAGE_API_KEY:
            raise HTTPException(status_code=500, detail="VOYAGE_API_KEY is not set")

        payload = {
            "input": texts,
            "model": MODEL_NAME,
        }
        if input_type is not None:
            payload["input_type"] = input_type
        payload["output_dimension"] = output_dimension or VOYAGE_OUTPUT_DIMENSION
        if output_dtype is not None:
            payload["output_dtype"] = output_dtype
        if truncation is not None:
            payload["truncation"] = truncation
        req = urllib.request.Request(
            f"{VOYAGE_BASE_URL}/v1/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {VOYAGE_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=VOYAGE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        status = e.code or 502
        logger.warning("Voyage error %s: %s", status, detail[:500])
        raise HTTPException(status_code=status, detail=detail[:500]) from e
    except urllib.error.URLError as e:
        logger.warning("Voyage transport error: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e

    embeddings = (data.get("data") or [])
    return [list(item["embedding"]) for item in embeddings]


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def openai_embeddings(body: EmbeddingRequest) -> EmbeddingResponse:
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts:
        raise HTTPException(status_code=400, detail="input is empty")
    if any(not t.strip() for t in texts):
        raise HTTPException(status_code=400, detail="empty text in input")

    started = time.perf_counter()
    vectors = _embed_texts(
        texts,
        input_type=body.input_type,
        output_dimension=body.output_dimension,
        output_dtype=body.output_dtype,
        truncation=body.truncation,
    )
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
    vectors = _embed_texts(
        body.texts,
        input_type=body.input_type,
        output_dimension=body.output_dimension,
        output_dtype=body.output_dtype,
        truncation=body.truncation,
    )
    dims = len(vectors[0]) if vectors else 0
    return EmbedBatchResponse(model=MODEL_NAME, dimensions=dims, embeddings=vectors)


@app.get("/info")
def info() -> dict[str, Any]:
    return {
        "model": MODEL_NAME,
        "dimensions": VOYAGE_OUTPUT_DIMENSION,
        "output_dtype": VOYAGE_OUTPUT_DTYPE,
        "base_url": VOYAGE_BASE_URL,
        "request_id": str(uuid.uuid4()),
    }

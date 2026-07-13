"""
Console smoke-test for the MoveToRussia RAG stack.

Usage:
  python scripts/rag_console_test.py "How long does the TRP process take?"
  python scripts/rag_console_test.py --no-answer

It embeds the question through the local Voyage proxy, searches Qdrant,
prints retrieved context, and optionally asks DeepSeek to synthesize an answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> Any:
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e


def embed_query(embedder_url: str, question: str) -> list[float]:
    resp = http_json(
        "POST",
        f"{embedder_url.rstrip('/')}/embed",
        {"texts": [question], "input_type": "query"},
        timeout=300,
    )
    embeddings = resp.get("embeddings") or []
    if not embeddings:
        raise RuntimeError("No embedding returned for the question")
    return embeddings[0]


def search_qdrant(
    qdrant_url: str,
    collection: str,
    vector: list[float],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    resp = http_json(
        "POST",
        f"{qdrant_url.rstrip('/')}/collections/{collection}/points/search",
        {
            "vector": vector,
            "limit": top_k,
            "with_payload": True,
        },
        timeout=120,
    )
    return resp.get("result") or []


def format_context(hits: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for i, hit in enumerate(hits, start=1):
        payload = hit.get("payload") or {}
        src = payload.get("source_type", "unknown")
        score = hit.get("score", 0)
        meta_bits = []
        for key in ("client_email", "thread_file", "theme", "frequency", "subject", "date"):
            value = payload.get(key)
            if value:
                meta_bits.append(f"{key}={value}")
        header = f"[{i}] {src} | score={score:.4f}"
        if meta_bits:
            header += " | " + "; ".join(meta_bits)
        blocks.append(f"{header}\n{payload.get('content', '')}")
    return "\n\n---\n\n".join(blocks)


def answer_with_deepseek(
    question: str,
    context: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
) -> str:
    system_prompt = (
        "You are an internal MovetoRussia knowledge assistant for managers. "
        "Answer only from the provided context. If the context does not contain "
        "the answer, say so clearly. Do not invent facts, prices, timelines or links."
    )
    user_prompt = f"Context:\n---\n{context}\n---\n\nQuestion: {question}"
    resp = http_json(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=180,
    )
    answer = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
    return answer.strip()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("question", nargs="?", help="Question to test")
    p.add_argument("--no-answer", action="store_true", help="Only show retrieved chunks")
    p.add_argument("--top-k", type=int, default=int(_env("RAG_TOP_K", "6")))
    p.add_argument("--qdrant-url", default=_env("QDRANT_URL", "http://localhost:6333"))
    p.add_argument("--embedder-url", default=_env("EMBEDDER_URL", "http://localhost:8081"))
    p.add_argument("--collection", default=_env("RAG_COLLECTION", "movetorussia_kb"))
    p.add_argument("--deepseek-model", default=_env("DEEPSEEK_MODEL", "deepseek-chat"))
    p.add_argument("--deepseek-base-url", default=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "rag" / ".env")
    args = parse_args()
    question = args.question or input("Question: ").strip()
    if not question:
        print("Empty question")
        return 1

    print("=== RAG smoke test ===")
    print(f"Qdrant:   {args.qdrant_url}")
    print(f"Embedder: {args.embedder_url}")
    print(f"Collection: {args.collection}")
    print()

    vector = embed_query(args.embedder_url, question)
    hits = search_qdrant(args.qdrant_url, args.collection, vector, top_k=args.top_k)

    if not hits:
        print("No retrieval results.")
        return 0

    print("Top retrieved context:\n")
    context = format_context(hits)
    print(context)

    if args.no_answer:
        return 0

    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        print("\nDEEPSEEK_API_KEY is not set; skipping generation.")
        return 0

    print("\n=== DeepSeek answer ===\n")
    answer = answer_with_deepseek(
        question,
        context,
        model=args.deepseek_model,
        base_url=args.deepseek_base_url,
        api_key=api_key,
    )
    print(answer or "(empty answer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

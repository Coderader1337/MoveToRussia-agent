#!/usr/bin/env python3
"""Compare prod bot answers vs experimental KB (standard corpus + Telegram chats).

Runs the same RAG pipeline as the prod/dev Telegram bot (extract questions ->
Voyage embed -> Qdrant top-k -> DeepSeek), but as a batch CLI instead of
sending replies to Telegram.

Retrieval uses the experimental Qdrant collection (movetorussia_kb_exp):
a clone of the prod knowledge base with Telegram client conversations indexed
on top. Input questions come from prod bot usage_stats.csv; output CSV columns:
question, answer (prod bot at the time), exp_answer (this run against _exp KB).

Each row is processed independently (no conversation context). Voyage embed
calls are rate-limited (default 20 s between calls = 3 RPM).

Usage:
    python scripts/run_exp_batch.py \\
        --input-csv /opt/movetorussia/rag/data/usage_stats.csv \\
        --output-csv data/exp_comparison.csv

    python scripts/run_exp_batch.py --limit 3
    python scripts/run_exp_batch.py --resume
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_exp_batch")

OUTPUT_HEADER = ("question", "answer", "exp_answer")

DEFAULT_QDRANT_URL = "http://localhost:6335"
DEFAULT_QDRANT_COLLECTION = "movetorussia_kb_exp"
# Voyage free tier: 3 requests/minute -> at least 20 s between embed API calls.
DEFAULT_VOYAGE_MIN_INTERVAL = 20.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--input-csv",
        type=Path,
        default=None,
        help="Prod bot stats CSV (default: MTR_STATS_CSV_PATH or rag/data/usage_stats.csv)",
    )
    p.add_argument(
        "--output-csv",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "exp_comparison.csv",
        help="Comparison output CSV path",
    )
    p.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL,
                   help="Experimental Qdrant URL (prod clone + Telegram, port 6335)")
    p.add_argument(
        "--qdrant-collection",
        default=DEFAULT_QDRANT_COLLECTION,
        help="Experimental collection: standard KB + Telegram embeddings (_exp suffix)",
    )
    p.add_argument("--top-k", type=int, default=None, help="Retrieval top-k (default from settings)")
    p.add_argument("--limit", type=int, default=None, help="Process only the first N input rows")
    p.add_argument("--offset", type=int, default=0, help="Skip the first N input rows")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip input rows whose question is already present in the output CSV",
    )
    p.add_argument(
        "--voyage-min-interval",
        type=float,
        default=DEFAULT_VOYAGE_MIN_INTERVAL,
        help="Minimum seconds between Voyage embed API calls (default: 20 = 3 RPM)",
    )
    return p.parse_args()


def apply_qdrant_override(url: str, collection: str) -> None:
    """Point retrieval at movetorussia_kb_exp (prod corpus + Telegram chats)."""
    import mtr_rag.config as config_mod

    config_mod.settings = dataclasses.replace(
        config_mod.settings,
        qdrant_url=url,
        qdrant_collection=collection,
    )


def load_input_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Input CSV is empty or has no header: {path}")
        missing = [c for c in ("question", "answer") if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"Input CSV missing columns {missing}: {path}")

        for row in reader:
            question = (row.get("question") or "").strip()
            if not question:
                continue
            rows.append(
                {
                    "question": question,
                    "answer": (row.get("answer") or "").strip(),
                }
            )
    return rows


def load_completed_questions(path: Path) -> set[str]:
    if not path.is_file() or path.stat().st_size == 0:
        return set()

    done: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "question" not in reader.fieldnames:
            return set()
        for row in reader:
            question = (row.get("question") or "").strip()
            exp_answer = (row.get("exp_answer") or "").strip()
            if question and exp_answer and not exp_answer.startswith("[ERROR:"):
                done.add(question)
    return done


def open_output_writer(path: Path, *, resume: bool) -> tuple[csv.writer, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not resume or not path.is_file() or path.stat().st_size == 0
    f = path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
    if write_header:
        writer.writerow(OUTPUT_HEADER)
        f.flush()
    return writer, f


def resolve_input_csv(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    from mtr_rag.config import settings

    prod_default = Path("/opt/movetorussia/rag/data/usage_stats.csv")
    if prod_default.is_file():
        return prod_default
    return settings.stats_csv_path


def build_rate_limited_embedder(min_interval: float):
    from mtr_rag.embeddings import VoyageEmbedder

    class _RateLimitedVoyageEmbedder(VoyageEmbedder):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._min_interval = min_interval
            self._last_call = 0.0
            self._interval_lock = threading.Lock()

        def _wait_for_slot(self) -> None:
            with self._interval_lock:
                now = time.monotonic()
                if self._last_call > 0:
                    wait = self._min_interval - (now - self._last_call)
                    if wait > 0:
                        logger.info("Voyage rate limit: sleeping %.1fs", wait)
                        time.sleep(wait)
                self._last_call = time.monotonic()

        def _embed_with_retry(self, texts: list[str], input_type: str) -> list[list[float]]:
            self._wait_for_slot()
            return super()._embed_with_retry(texts, input_type)

    return _RateLimitedVoyageEmbedder()


def main() -> int:
    args = parse_args()

    import mtr_rag.config as config_mod  # noqa: F401 — loads .env and settings

    apply_qdrant_override(args.qdrant_url, args.qdrant_collection)

    from mtr_rag.chain import ask, warmup_prompt  # noqa: E402

    input_csv = resolve_input_csv(args.input_csv)
    rows = load_input_rows(input_csv)
    if args.offset:
        rows = rows[args.offset :]
    if args.limit is not None:
        rows = rows[: args.limit]

    completed = load_completed_questions(args.output_csv) if args.resume else set()
    if completed:
        before = len(rows)
        rows = [r for r in rows if r["question"] not in completed]
        logger.info("Resume: skipping %d already completed rows", before - len(rows))

    if not rows:
        logger.info("Nothing to process.")
        return 0

    warmup_prompt()
    embedder = build_rate_limited_embedder(args.voyage_min_interval)
    top_k = args.top_k or config_mod.settings.retrieval_top_k

    logger.info(
        "Starting batch: input=%s rows=%d output=%s qdrant=%s collection=%s top_k=%d voyage_interval=%.1fs",
        input_csv,
        len(rows),
        args.output_csv,
        args.qdrant_url,
        args.qdrant_collection,
        top_k,
        args.voyage_min_interval,
    )

    writer, out_file = open_output_writer(args.output_csv, resume=args.resume)
    processed, errors = 0, 0

    try:
        for idx, row in enumerate(rows, start=1):
            question = row["question"]
            prod_answer = row["answer"]
            logger.info("Processing %d/%d: %s", idx, len(rows), question[:120])

            try:
                result = ask(question, top_k=top_k, embedder=embedder)
                exp_answer = result.answer.strip()
                if not exp_answer:
                    exp_answer = "[ERROR: empty model response]"
                    errors += 1
            except Exception as exc:
                logger.exception("Failed on row %d", idx)
                exp_answer = f"[ERROR: {type(exc).__name__}]"
                errors += 1

            writer.writerow([question, prod_answer, exp_answer])
            out_file.flush()
            processed += 1
    finally:
        out_file.close()

    logger.info(
        "Done. processed=%d errors=%d output=%s",
        processed,
        errors,
        args.output_csv,
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

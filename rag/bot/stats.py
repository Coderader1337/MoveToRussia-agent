"""Append-only CSV log of bot usage (one row per rated question)."""

from __future__ import annotations

import csv
import threading
from pathlib import Path

from mtr_rag.config import settings

_lock = threading.Lock()
_HEADER = ("timestamp", "telegram_id", "question", "answer", "rate")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_usage_row(
    *,
    timestamp: str,
    telegram_id: int,
    question: str,
    answer: str,
    rate: int,
) -> Path:
    """Append one usage record. Thread-safe for concurrent managers."""
    path = settings.stats_csv_path
    _ensure_parent(path)
    row = (timestamp, telegram_id, question, answer, rate)
    with _lock:
        write_header = not path.is_file() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            if write_header:
                writer.writerow(_HEADER)
            writer.writerow(row)
    return path

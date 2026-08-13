"""Load raw data sources into the unified `Chunk` schema.

- `iter_corpus_chunks`: mailbox_export_RAG/corpus.jsonl (one JSON object per line)
- `iter_faq_chunks`: knowledge_base/v4/client_faq_review.csv (semicolon-delimited FAQ catalog)
- `iter_disk_corpus_chunks`: data/yandex_disk/corpus.jsonl (txt files from Yandex Disk)
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

from .schema import Chunk


def iter_corpus_chunks(corpus_path: Path) -> Iterator[Chunk]:
    if not corpus_path.is_file():
        raise FileNotFoundError(f"Corpus file not found: {corpus_path}")
    with corpus_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            yield Chunk(
                id=row["id"],
                thread_id=row.get("thread_id", ""),
                source=row.get("source", "mailbox_thread"),
                text=row.get("text", "") or "",
                subject=row.get("subject", "") or "",
                client_email=row.get("client_email"),
                manager_emails=row.get("manager_emails", []) or [],
                date_start=row.get("date_start"),
                date_end=row.get("date_end"),
                language=row.get("language"),
                low_signal=bool(row.get("low_signal", False)),
                word_count=row.get("word_count", 0),
                distilled=row.get("distilled"),
            )


def iter_faq_chunks(faq_csv_path: Path) -> Iterator[Chunk]:
    """FAQ catalog produced by scripts/build_faq_catalog.py (client_faq_review.csv).

    Columns: number;theme;theme_label;frequency;languages;question;
             question_original;answer;variants
    """
    if not faq_csv_path.is_file():
        raise FileNotFoundError(f"FAQ catalog not found: {faq_csv_path}")
    with faq_csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            question = (row.get("question") or "").strip()
            answer = (row.get("answer") or "").strip()
            if not question or not answer:
                continue
            number = (row.get("number") or "0").strip()
            theme = (row.get("theme") or "other").strip()
            theme_label = (row.get("theme_label") or "Other").strip()
            languages = (row.get("languages") or "en").strip()
            text = f"Q: {question}\nA: {answer}"
            yield Chunk(
                id=f"faq_{theme}_{number}",
                thread_id=f"faq_catalog:{theme}",
                source="faq_catalog",
                text=text,
                subject=theme_label,
                client_email=None,
                manager_emails=[],
                date_start=None,
                date_end=None,
                language=languages.split(",")[0].strip() if languages else "en",
                low_signal=False,
                word_count=len(text.split()),
                distilled=None,
                extra={
                    "theme": theme,
                    "frequency": int(row.get("frequency") or 0),
                    "question": question,
                    "answer": answer,
                },
            )


def iter_disk_corpus_chunks(corpus_path: Path) -> Iterator[Chunk]:
    """Supplemental txt corpus synced from Yandex Disk (source=yandex_disk)."""
    if not corpus_path.is_file():
        return
    with corpus_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            yield Chunk(
                id=row["id"],
                thread_id=row.get("thread_id", ""),
                source=row.get("source", "yandex_disk"),
                text=row.get("text", "") or "",
                subject=row.get("subject", "") or "",
                client_email=row.get("client_email"),
                manager_emails=row.get("manager_emails", []) or [],
                date_start=row.get("date_start"),
                date_end=row.get("date_end"),
                language=row.get("language"),
                low_signal=bool(row.get("low_signal", False)),
                word_count=row.get("word_count", 0),
                distilled=row.get("distilled"),
                extra=row.get("extra", {}) or {},
            )


def load_all_chunks(
    corpus_path: Path,
    faq_csv_path: Path,
    *,
    include_faq: bool = True,
    disk_corpus_path: Path | None = None,
) -> list[Chunk]:
    chunks = list(iter_corpus_chunks(corpus_path))
    if include_faq:
        try:
            chunks.extend(iter_faq_chunks(faq_csv_path))
        except FileNotFoundError:
            pass
    if disk_corpus_path is not None:
        chunks.extend(iter_disk_corpus_chunks(disk_corpus_path))
    return chunks

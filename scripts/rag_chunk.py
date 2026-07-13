"""
Chunking utilities for MoveToRussia RAG (mailbox_export_clean + text uploads).

Источник переписок: mailbox_export_clean/threads/*.txt
"""

from __future__ import annotations

import csv
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
CLEAN_THREADS_DIR = ROOT / "mailbox_export_clean" / "threads"
FAQ_REVIEW_CSV = ROOT / "knowledge_base" / "v4" / "client_faq_review.csv"

MSG_SEP = re.compile(r"={10,}")


@dataclass
class RagChunk:
    chunk_id: str
    content: str
    metadata: dict[str, Any]


def stable_chunk_id(*parts: str) -> str:
    """Детерминированный UUID для Qdrant (строковый id)."""
    raw = "|".join(p.strip() for p in parts if p)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def format_message_chunk(msg: dict[str, Any], *, thread_file: str = "") -> RagChunk:
    direction = msg.get("direction", "")
    role = "CLIENT" if direction == "incoming" else "MANAGER"
    client = msg.get("counterpart", "")
    subject = msg.get("subject", "")
    date = msg.get("date", "")
    body = (msg.get("text") or "").strip()
    mailbox = msg.get("mailbox_account", "")

    content = (
        f"[{role}] Client: {client}\n"
        f"Subject: {subject}\n"
        f"Date: {date}\n"
        f"Manager mailbox: {mailbox}\n\n"
        f"{body}"
    ).strip()

    chunk_id = stable_chunk_id("message", client, date, subject, body[:200])
    metadata = {
        "source_type": "mailbox_thread",
        "chunk_kind": "email_message",
        "client_email": client,
        "direction": direction,
        "role": role.lower(),
        "subject": subject[:500],
        "date": date[:120],
        "manager_mailbox": mailbox,
        "thread_file": thread_file,
    }
    return RagChunk(chunk_id=chunk_id, content=content, metadata=metadata)


def iter_message_chunks_from_clean_threads(
    threads_dir: Path = CLEAN_THREADS_DIR,
) -> Iterator[RagChunk]:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from build_knowledge_base import load_messages_from_clean_threads  # noqa: WPS433

    if not threads_dir.is_dir():
        raise FileNotFoundError(f"Нет каталога {threads_dir}")

    thread_files = {p.stem: p.name for p in threads_dir.glob("*.txt")}
    messages = load_messages_from_clean_threads(threads_dir)

    for msg in messages:
        client = msg.get("counterpart", "")
        stem = client.replace("@", "_").replace(".", "_")
        thread_file = thread_files.get(stem, "")
        if not (msg.get("text") or "").strip():
            continue
        yield format_message_chunk(msg, thread_file=thread_file)


def iter_faq_chunks(csv_path: Path = FAQ_REVIEW_CSV) -> Iterator[RagChunk]:
    if not csv_path.is_file():
        return

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            question = (row.get("question") or "").strip()
            answer = (row.get("answer") or "").strip()
            theme = (row.get("theme_label") or row.get("theme") or "").strip()
            frequency = (row.get("frequency") or "").strip()
            if not question or not answer:
                continue

            content = (
                f"[FAQ] Theme: {theme}\n"
                f"Frequency: {frequency}\n\n"
                f"Question: {question}\n\n"
                f"Answer: {answer}"
            ).strip()
            chunk_id = stable_chunk_id("faq", question, answer[:300])
            yield RagChunk(
                chunk_id=chunk_id,
                content=content,
                metadata={
                    "source_type": "faq_catalog",
                    "chunk_kind": "faq",
                    "theme": theme[:200],
                    "frequency": frequency,
                    "question": question[:1000],
                },
            )


def split_upload_text(
    text: str,
    *,
    source_name: str = "upload.txt",
    max_chars: int = 1800,
    overlap: int = 200,
) -> list[RagChunk]:
    """Разбить произвольный текст (загрузка через Telegram) на чанки."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    # Если файл в формате thread.txt — парсим по сообщениям.
    if text.startswith("КЛИЕНТ:") and "======" in text:
        return _chunks_from_thread_upload(text, source_name)

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[RagChunk] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}".strip() if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(_upload_chunk(buf, source_name, len(chunks)))
        if len(para) <= max_chars:
            buf = para
        else:
            chunks.extend(_split_long_text(para, source_name, max_chars, overlap, len(chunks)))
            buf = ""

    if buf:
        chunks.append(_upload_chunk(buf, source_name, len(chunks)))
    return chunks


def _split_long_text(
    text: str,
    source_name: str,
    max_chars: int,
    overlap: int,
    start_idx: int,
) -> list[RagChunk]:
    out: list[RagChunk] = []
    start = 0
    idx = start_idx
    while start < len(text):
        end = min(start + max_chars, len(text))
        piece = text[start:end].strip()
        if piece:
            out.append(_upload_chunk(piece, source_name, idx))
            idx += 1
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def _upload_chunk(text: str, source_name: str, idx: int) -> RagChunk:
    chunk_id = stable_chunk_id("upload", source_name, str(idx), text[:200])
    return RagChunk(
        chunk_id=chunk_id,
        content=f"[UPLOAD] Source: {source_name}\n\n{text}",
        metadata={
            "source_type": "telegram_upload",
            "chunk_kind": "text_upload",
            "source_name": source_name,
            "chunk_index": idx,
        },
    )


def _chunks_from_thread_upload(text: str, source_name: str) -> list[RagChunk]:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from clean_thread_quotes import parse_thread_file  # noqa: WPS433

    client, _, blocks = parse_thread_file(text)
    if not client:
        return split_upload_text(text, source_name=source_name)

    out: list[RagChunk] = []
    for block in blocks:
        msg = {
            "counterpart": client.strip().lower(),
            "direction": "incoming" if "КЛИЕНТ →" in (block.header_lines[1] if len(block.header_lines) > 1 else "") else "outgoing",
            "subject": next((ln.split(":", 1)[1].strip() for ln in block.header_lines if ln.startswith("Тема:")), ""),
            "date": next((ln.split(":", 1)[1].strip() for ln in block.header_lines if ln.startswith("Дата:")), ""),
            "mailbox_account": "",
            "text": block.body.strip(),
        }
        if msg["text"]:
            chunk = format_message_chunk(msg, thread_file=source_name)
            chunk.metadata["source_type"] = "telegram_upload"
            out.append(chunk)
    return out


def e5_passage(text: str) -> str:
    return f"passage: {text}"


def e5_query(text: str) -> str:
    return f"query: {text}"

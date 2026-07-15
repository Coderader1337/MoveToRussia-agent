"""Unified chunk record shared by mailbox corpus and FAQ catalog loaders.

Both sources are normalized into this shape before embedding/indexing, so the
Qdrant payload always has the same fields regardless of `source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    id: str
    thread_id: str
    source: str  # "mailbox_thread" | "faq_catalog"
    text: str
    subject: str = ""
    client_email: str | None = None
    manager_emails: list[str] = field(default_factory=list)
    date_start: str | None = None
    date_end: str | None = None
    language: str | None = None
    low_signal: bool = False
    word_count: int = 0
    distilled: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def embedding_text(self) -> str:
        """Text actually sent to the embedding model.

        Prepending subject/source context tends to improve retrieval quality
        for short chunks without materially changing long ones.
        """
        parts = []
        if self.subject:
            parts.append(f"Subject: {self.subject}")
        parts.append(self.text)
        return "\n\n".join(parts)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "thread_id": self.thread_id,
            "source": self.source,
            "text": self.text,
            "subject": self.subject,
            "client_email": self.client_email,
            "manager_emails": self.manager_emails,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "language": self.language,
            "low_signal": self.low_signal,
            "word_count": self.word_count,
            "distilled": self.distilled,
        }
        payload.update(self.extra)
        return payload

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> "Chunk":
        known = {
            "id", "thread_id", "source", "text", "subject", "client_email",
            "manager_emails", "date_start", "date_end", "language",
            "low_signal", "word_count", "distilled",
        }
        extra = {k: v for k, v in payload.items() if k not in known}
        return Chunk(
            id=payload.get("id", ""),
            thread_id=payload.get("thread_id", ""),
            source=payload.get("source", "mailbox_thread"),
            text=payload.get("text", ""),
            subject=payload.get("subject", ""),
            client_email=payload.get("client_email"),
            manager_emails=payload.get("manager_emails", []) or [],
            date_start=payload.get("date_start"),
            date_end=payload.get("date_end"),
            language=payload.get("language"),
            low_signal=bool(payload.get("low_signal", False)),
            word_count=payload.get("word_count", 0),
            distilled=payload.get("distilled"),
            extra=extra,
        )

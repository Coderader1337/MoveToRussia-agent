"""Tests for Yandex Disk priority retrieval merge."""
from __future__ import annotations

from langchain_core.documents import Document

from mtr_rag.retriever import merge_disk_priority


def _doc(source: str, thread_id: str, score: float, text: str = "x") -> Document:
    return Document(page_content=text, metadata={"source": source, "thread_id": thread_id, "score": score})


def test_merge_reserves_relevant_disk_chunk():
    general = [
        _doc("mailbox_thread", "t1", 0.37),
        _doc("mailbox_thread", "t2", 0.36),
        _doc("mailbox_thread", "t3", 0.35),
    ]
    disk = [
        _doc("yandex_disk", "ydisk__white_gloves", 0.354, "White Gloves package"),
        _doc("yandex_disk", "ydisk__space", 0.04, "Moon flight"),
    ]
    merged = merge_disk_priority(
        general, disk, top_k=3, disk_reserve_slots=2, disk_min_score=0.30
    )
    assert len(merged) == 3
    assert merged[0].metadata["source"] == "yandex_disk"
    assert merged[0].metadata["thread_id"] == "ydisk__white_gloves"
    assert all(d.metadata["source"] == "mailbox_thread" for d in merged[1:])


def test_merge_skips_irrelevant_disk_chunk():
    general = [_doc("mailbox_thread", "t1", 0.37)]
    disk = [_doc("yandex_disk", "ydisk__space", 0.04, "Moon flight")]
    merged = merge_disk_priority(
        general, disk, top_k=2, disk_reserve_slots=2, disk_min_score=0.30
    )
    assert len(merged) == 1
    assert merged[0].metadata["source"] == "mailbox_thread"

"""Sync txt files from a Yandex Disk folder into a local mirror + corpus.jsonl.

Each .txt file becomes one chunk (source=yandex_disk). The sync job keeps only
the current remote state: updated files overwrite, removed files are deleted
locally, and the manifest diff drives Qdrant upsert/delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yadisk

from .config import settings
from .schema import Chunk

logger = logging.getLogger(__name__)

DISK_SOURCE = "yandex_disk"
FILES_SUBDIR = "files"
CORPUS_FILENAME = "corpus.jsonl"
MANIFEST_FILENAME = "manifest.json"


@dataclass
class ManifestEntry:
    chunk_id: str
    file_path: str
    content_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "content_sha256": self.content_sha256,
        }

    @staticmethod
    def from_dict(data: dict) -> ManifestEntry:
        return ManifestEntry(
            chunk_id=data["chunk_id"],
            file_path=data["file_path"],
            content_sha256=data["content_sha256"],
        )


@dataclass
class SyncDiff:
    added: list[ManifestEntry] = field(default_factory=list)
    changed: list[ManifestEntry] = field(default_factory=list)
    removed: list[ManifestEntry] = field(default_factory=list)

    @property
    def to_upsert(self) -> list[ManifestEntry]:
        return self.added + self.changed


def corpus_dir(base: Path | None = None) -> Path:
    return base or settings.disk_corpus_dir


def files_dir(base: Path | None = None) -> Path:
    return corpus_dir(base) / FILES_SUBDIR


def corpus_jsonl_path(base: Path | None = None) -> Path:
    return corpus_dir(base) / CORPUS_FILENAME


def manifest_path(base: Path | None = None) -> Path:
    return corpus_dir(base) / MANIFEST_FILENAME


def _slugify_path(relative_path: str) -> str:
    """Turn a relative file path into a stable chunk-id suffix."""
    slug = relative_path.replace("\\", "/").lower()
    slug = re.sub(r"[^a-z0-9._/-]+", "_", slug)
    slug = slug.strip("/").replace("/", "__")
    return slug or "root"


def chunk_id_for_file(relative_path: str) -> str:
    return f"ydisk__{_slugify_path(relative_path)}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_disk_path(path: str) -> str:
    if path.startswith("disk:"):
        return path[5:]
    return path


def _relative_remote_path(remote_path: str, remote_dir: str) -> str:
    path = _normalize_disk_path(remote_path)
    prefix = _normalize_disk_path(remote_dir.rstrip("/"))
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    if path == prefix:
        return ""
    if path.startswith(prefix + "/"):
        return path[len(prefix) + 1 :]
    raise ValueError(f"Remote path {remote_path!r} is not under {remote_dir!r}")


def _iter_remote_txt_files(client: yadisk.Client, remote_dir: str) -> list[tuple[str, str]]:
    """Return (remote_path, relative_path) for every .txt under remote_dir."""
    remote_dir = remote_dir.rstrip("/") or "/"
    if not client.exists(remote_dir):
        raise FileNotFoundError(f"Yandex Disk folder not found: {remote_dir}")

    results: list[tuple[str, str]] = []

    def walk(path: str) -> None:
        for item in client.listdir(path):
            item_path = item.path
            if item.type == "dir":
                walk(item_path)
            elif item.type == "file" and item_path.lower().endswith(".txt"):
                rel = _relative_remote_path(item_path, remote_dir)
                if rel:
                    results.append((item_path, rel))

    walk(remote_dir)
    results.sort(key=lambda x: x[1])
    return results


def sync_from_yandex_disk(
    *,
    token: str | None = None,
    remote_dir: str | None = None,
    base_dir: Path | None = None,
    persist: bool = True,
) -> tuple[SyncDiff, dict[str, ManifestEntry]]:
    """Download current .txt files from Yandex Disk, rebuild corpus.jsonl, return diff + entries."""
    token = token or settings.yandex_disk_token
    remote_dir = remote_dir or settings.yandex_disk_remote_dir
    base = corpus_dir(base_dir)
    local_files = files_dir(base_dir)
    local_files.mkdir(parents=True, exist_ok=True)

    if not token:
        raise ValueError("YANDEX_DISK_TOKEN is not set")

    old_manifest = load_manifest(base_dir)

    if not persist:
        with yadisk.Client(token=token) as client:
            if not client.check_token():
                raise ValueError("Yandex Disk token is invalid or expired")
            new_entries = _preview_remote_entries(client, remote_dir)
        diff = compute_diff(old_manifest, new_entries)
        logger.info(
            "Preview complete: added=%d changed=%d removed=%d total=%d",
            len(diff.added), len(diff.changed), len(diff.removed), len(new_entries),
        )
        return diff, new_entries

    new_entries: dict[str, ManifestEntry] = {}

    with yadisk.Client(token=token) as client:
        if not client.check_token():
            raise ValueError("Yandex Disk token is invalid or expired")

        remote_files = _iter_remote_txt_files(client, remote_dir)
        logger.info("Found %d .txt file(s) on Yandex Disk at %s", len(remote_files), remote_dir)

        for remote_path, rel_path in remote_files:
            dest = local_files / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download(remote_path, str(dest))
            content_hash = sha256_file(dest)
            chunk_id = chunk_id_for_file(rel_path)
            new_entries[chunk_id] = ManifestEntry(
                chunk_id=chunk_id,
                file_path=rel_path.replace("\\", "/"),
                content_sha256=content_hash,
            )

    # Remove local files that no longer exist on the remote folder.
    for local_path in local_files.rglob("*.txt"):
        rel = local_path.relative_to(local_files).as_posix()
        if rel not in {e.file_path for e in new_entries.values()}:
            local_path.unlink()
            logger.info("Removed local orphan: %s", rel)
    for directory in sorted(local_files.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass

    _write_corpus_jsonl(local_files, corpus_jsonl_path(base_dir))
    diff = compute_diff(old_manifest, new_entries)
    save_manifest(new_entries, base_dir)
    logger.info(
        "Sync complete: added=%d changed=%d removed=%d total=%d",
        len(diff.added), len(diff.changed), len(diff.removed), len(new_entries),
    )
    return diff, new_entries


def _scan_local_entries(base_dir: Path | None = None) -> dict[str, ManifestEntry]:
    local_files = files_dir(base_dir)
    entries: dict[str, ManifestEntry] = {}
    if local_files.is_dir():
        for path in sorted(local_files.rglob("*.txt")):
            rel = path.relative_to(local_files).as_posix()
            entries[chunk_id_for_file(rel)] = ManifestEntry(
                chunk_id=chunk_id_for_file(rel),
                file_path=rel,
                content_sha256=sha256_file(path),
            )
    return entries


def _preview_remote_entries(
    client: yadisk.Client,
    remote_dir: str,
) -> dict[str, ManifestEntry]:
    """Build manifest entries from remote metadata only (no download)."""
    entries: dict[str, ManifestEntry] = {}
    for remote_path, rel_path in _iter_remote_txt_files(client, remote_dir):
        meta = client.get_meta(remote_path)
        fingerprint = meta.md5 or f"size:{meta.size}:modified:{meta.modified}"
        entries[chunk_id_for_file(rel_path)] = ManifestEntry(
            chunk_id=chunk_id_for_file(rel_path),
            file_path=rel_path.replace("\\", "/"),
            content_sha256=fingerprint,
        )
    return entries


def build_corpus_from_local(*, base_dir: Path | None = None, persist: bool = True) -> dict[str, ManifestEntry]:
    """Rebuild corpus.jsonl + manifest from the local files/ mirror (no network)."""
    entries = _scan_local_entries(base_dir)
    if persist:
        _write_corpus_jsonl(files_dir(base_dir), corpus_jsonl_path(base_dir))
        save_manifest(entries, base_dir)
    return entries


def _write_corpus_jsonl(local_files: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as out:
        if not local_files.is_dir():
            return
        for path in sorted(local_files.rglob("*.txt")):
            rel = path.relative_to(local_files).as_posix()
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            record = {
                "id": chunk_id_for_file(rel),
                "thread_id": f"yandex_disk:{rel}",
                "source": DISK_SOURCE,
                "text": text,
                "subject": Path(rel).stem,
                "client_email": None,
                "manager_emails": [],
                "date_start": None,
                "date_end": None,
                "language": None,
                "low_signal": False,
                "word_count": len(text.split()),
                "distilled": None,
                "extra": {
                    "file_path": rel,
                    "content_sha256": sha256_text(text),
                },
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_manifest(base_dir: Path | None = None) -> dict[str, ManifestEntry]:
    path = manifest_path(base_dir)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["chunk_id"]: ManifestEntry.from_dict(item) for item in data.get("files", [])}


def save_manifest(entries: dict[str, ManifestEntry], base_dir: Path | None = None) -> None:
    path = manifest_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "files": [entries[cid].to_dict() for cid in sorted(entries)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compute_diff(
    old: dict[str, ManifestEntry],
    new: dict[str, ManifestEntry],
) -> SyncDiff:
    diff = SyncDiff()
    for chunk_id, entry in new.items():
        prev = old.get(chunk_id)
        if prev is None:
            diff.added.append(entry)
        elif prev.content_sha256 != entry.content_sha256:
            diff.changed.append(entry)
    for chunk_id, entry in old.items():
        if chunk_id not in new:
            diff.removed.append(entry)
    return diff


def load_disk_chunks(base_dir: Path | None = None) -> list[Chunk]:
    """Load chunks from the generated corpus.jsonl."""
    path = corpus_jsonl_path(base_dir)
    if not path.is_file():
        return []
    chunks: list[Chunk] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            chunks.append(
                Chunk(
                    id=row["id"],
                    thread_id=row.get("thread_id", ""),
                    source=row.get("source", DISK_SOURCE),
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
            )
    return chunks


def chunks_for_entries(entries: list[ManifestEntry], base_dir: Path | None = None) -> list[Chunk]:
    """Return Chunk objects for the given manifest entries."""
    by_id = {c.id: c for c in load_disk_chunks(base_dir)}
    return [by_id[e.chunk_id] for e in entries if e.chunk_id in by_id]

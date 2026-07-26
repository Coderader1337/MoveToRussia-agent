"""Telegram user whitelist loaded from JSON (user IDs only)."""

from __future__ import annotations

import json
import logging

from mtr_rag.config import settings

logger = logging.getLogger(__name__)

_ALLOWED_IDS: set[int] | None = None


def load_allowed_user_ids() -> set[int]:
    """Load and cache allowed Telegram user IDs from the whitelist JSON file."""
    global _ALLOWED_IDS
    if _ALLOWED_IDS is not None:
        return _ALLOWED_IDS

    path = settings.telegram_whitelist_path
    if not path.is_file():
        raise FileNotFoundError(f"Whitelist file not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        raw_ids = data
    elif isinstance(data, dict):
        raw_ids = data.get("allowed_user_ids", [])
    else:
        raise ValueError(f"Invalid whitelist format in {path}: expected list or object")

    ids = {int(uid) for uid in raw_ids}
    if not ids:
        raise ValueError(f"No user IDs found in whitelist: {path}")

    _ALLOWED_IDS = ids
    logger.info("Loaded %d allowed Telegram user IDs from %s", len(ids), path)
    return ids


def is_user_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in load_allowed_user_ids()

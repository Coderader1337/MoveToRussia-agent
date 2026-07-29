"""Per-user bot state: FSM for mandatory rating gate + lightweight preferences.

MemoryStorage is enough for a single bot instance on VPS (~10 managers).
Set REDIS_URL when running multiple replicas or when pending ratings must
survive restarts without losing the gate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from mtr_rag.config import settings

logger = logging.getLogger(__name__)

PENDING_QUESTION_KEY = "pending_question"
PENDING_ANSWER_KEY = "pending_answer"
PENDING_TIMESTAMP_KEY = "pending_timestamp"

_top_k_by_user: dict[int, int] = {}


class BotStates(StatesGroup):
    awaiting_rating = State()


def build_fsm_storage() -> BaseStorage:
    url = settings.redis_url.strip()
    if not url:
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage
        from redis.asyncio import Redis
    except ImportError as exc:
        raise RuntimeError(
            "REDIS_URL is set but the 'redis' package is not installed. "
            "Run: pip install redis"
        ) from exc
    logger.info("Using Redis FSM storage at %s", url.split("@")[-1])
    return RedisStorage(Redis.from_url(url))


def get_top_k(user_id: int) -> int | None:
    return _top_k_by_user.get(user_id)


def set_top_k(user_id: int, value: int) -> None:
    _top_k_by_user[user_id] = value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

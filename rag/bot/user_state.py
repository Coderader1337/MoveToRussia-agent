"""Per-user bot state: FSM for mandatory rating gate + lightweight preferences.

MemoryStorage is enough for a single bot instance on VPS (~10 managers).
Set REDIS_URL when running multiple replicas or when pending ratings must
survive restarts without losing the gate.

Память диалога (история треда для follow-up вопросов) хранится отдельно от
FSM-состояния: `state.clear()` в handle_rate обнуляет и state, и data FSM,
а тред должен переживать несколько раундов вопрос → ответ → оценка.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from mtr_rag.config import settings

logger = logging.getLogger(__name__)

PENDING_QUESTION_KEY = "pending_question"
PENDING_ANSWER_KEY = "pending_answer"
PENDING_TIMESTAMP_KEY = "pending_timestamp"

# Ответ обрезается перед сохранением в историю треда, чтобы не раздувать
# промпт follow-up запросов (полный текст черновика письма для памяти не нужен).
_HISTORY_ANSWER_MAX_CHARS = 600

_top_k_by_user: dict[int, int] = {}


class BotStates(StatesGroup):
    awaiting_rating = State()


@dataclass
class HistoryTurn:
    """Одна пара вопрос/ответ в треде диалога с менеджером."""

    question: str
    answer: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# user_id -> список последних реплик треда (самая новая — в конце).
_history_by_user: dict[int, list[HistoryTurn]] = {}


def _history_is_expired(turns: list[HistoryTurn]) -> bool:
    if not turns:
        return True
    ttl = settings.history_ttl_minutes
    if ttl <= 0:
        return False
    age = datetime.now(timezone.utc) - turns[-1].timestamp
    return age.total_seconds() > ttl * 60


def get_history(user_id: int) -> list[HistoryTurn]:
    """Вернуть историю треда пользователя, автоматически сбросив её по TTL."""
    turns = _history_by_user.get(user_id, [])
    if _history_is_expired(turns):
        _history_by_user.pop(user_id, None)
        return []
    return turns


def append_history_turn(user_id: int, question: str, answer: str) -> None:
    """Добавить пару вопрос/ответ в историю треда и обрезать до окна MTR_HISTORY_TURNS."""
    turns = _history_by_user.setdefault(user_id, [])
    if _history_is_expired(turns):
        turns.clear()
    trimmed_answer = answer.strip()
    if len(trimmed_answer) > _HISTORY_ANSWER_MAX_CHARS:
        trimmed_answer = trimmed_answer[:_HISTORY_ANSWER_MAX_CHARS].rstrip() + "…"
    turns.append(HistoryTurn(question=question.strip(), answer=trimmed_answer))
    max_turns = max(0, settings.history_turns)
    if max_turns:
        del turns[:-max_turns]
    else:
        turns.clear()


def reset_history(user_id: int) -> None:
    """Явный сброс треда (команда /reset)."""
    _history_by_user.pop(user_id, None)


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

"""Central configuration loaded from environment variables / .env.

Import `settings` from this module everywhere instead of reading os.environ
directly, so all defaults live in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# rag/.env takes priority; falls back to the repo-root .env if present.
_RAG_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _RAG_DIR.parent
load_dotenv(_REPO_ROOT / ".env")
load_dotenv(_RAG_DIR / ".env", override=True)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _resolve_existing_file(env_name: str, *candidates: Path) -> Path:
    override = os.getenv(env_name)
    if override:
        return Path(override)
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


@dataclass(frozen=True)
class Settings:
    # --- API keys ---
    voyage_api_key: str = os.getenv("VOYAGE_API_KEY", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # --- Voyage embeddings ---
    voyage_model: str = os.getenv("VOYAGE_MODEL", "voyage-4-large")
    voyage_output_dimension: int = _get_int("VOYAGE_OUTPUT_DIMENSION", 1024)
    voyage_batch_size: int = _get_int("VOYAGE_BATCH_SIZE", 16)

    # --- DeepSeek LLM ---
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    deepseek_temperature: float = _get_float("DEEPSEEK_TEMPERATURE", 0.2)

    # --- Qdrant ---
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "movetorussia_kb")

    # --- Data sources ---
    corpus_path: Path = Path(
        os.getenv("MTR_CORPUS_PATH", str(_REPO_ROOT / "mailbox_export_RAG" / "corpus.jsonl"))
    )
    faq_csv_path: Path = Path(
        os.getenv(
            "MTR_FAQ_CSV_PATH",
            str(_REPO_ROOT / "knowledge_base" / "v4" / "client_faq_review.csv"),
        )
    )

    # --- Yandex Disk supplemental corpus (txt files synced daily) ---
    yandex_disk_token: str = os.getenv("YANDEX_DISK_TOKEN", "")
    yandex_disk_remote_dir: str = os.getenv("YANDEX_DISK_REMOTE_DIR", "/rag_corpus")
    disk_corpus_dir: Path = Path(
        os.getenv("MTR_DISK_CORPUS_DIR", str(_RAG_DIR / "data" / "yandex_disk"))
    )

    # --- Retrieval defaults ---
    retrieval_top_k: int = _get_int("MTR_RETRIEVAL_TOP_K", 6)
    # Reserved slots for Yandex Disk official files when similarity exceeds disk_min_score.
    disk_reserve_slots: int = _get_int("MTR_DISK_RESERVE_SLOTS", 2)
    disk_min_score: float = _get_float("MTR_DISK_MIN_SCORE", 0.30)

    # --- Память диалога (контекст треда для follow-up вопросов) ---
    # Сколько последних пар вопрос/ответ хранить на пользователя.
    history_turns: int = _get_int("MTR_HISTORY_TURNS", 10)
    # Через сколько минут бездействия история треда сбрасывается автоматически.
    history_ttl_minutes: int = _get_int("MTR_HISTORY_TTL_MIN", 60)

    # --- Telegram bot access control ---
    telegram_whitelist_path: Path = Path(
        os.getenv(
            "MTR_TELEGRAM_WHITELIST_PATH",
            str(_RAG_DIR / "bot" / "allowed_users.json"),
        )
    )

    # --- Telegram bot usage stats (CSV, one row per rated request) ---
    stats_csv_path: Path = Path(
        os.getenv("MTR_STATS_CSV_PATH", str(_RAG_DIR / "data" / "usage_stats.csv"))
    )

    # Optional Redis for FSM when running multiple bot replicas (empty = in-memory).
    redis_url: str = os.getenv("REDIS_URL", "")

    # --- Mail-writing prompt (communication principles file) ---
    communication_principles_path: Path = _resolve_existing_file(
        "MTR_COMMUNICATION_PRINCIPLES_PATH",
        _RAG_DIR / "prompt_data" / "communication_principles.txt",
    )


settings = Settings()

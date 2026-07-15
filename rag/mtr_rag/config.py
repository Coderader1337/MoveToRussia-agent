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

    # --- Retrieval defaults ---
    retrieval_top_k: int = _get_int("MTR_RETRIEVAL_TOP_K", 6)


settings = Settings()

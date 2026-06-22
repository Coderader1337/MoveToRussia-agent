"""
Сборка базы знаний v1 из очищенных переписок (mailbox_export_clean).

Отдельная папка knowledge_base/v1_clean/ — старая knowledge_base/v1/ не трогается.

Параметры «максимального качества»:
  - все двусторонние диалоги v1 (max_threads=0)
  - map-бюджет 32 000 симв./диалог (vs 14 000 по умолчанию)
  - reduce-бюджет 100 000 симв./группа (vs 45 000)

Примеры:
  python build_v1_clean_kb.py --dry-run
  python build_v1_clean_kb.py
  python build_v1_clean_kb.py --force-llm
  python build_v1_clean_kb.py --force-llm --extract-kb
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import os
from pathlib import Path

os.environ.setdefault("PYTHONUNBUFFERED", "1")

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_kb_versions import (  # noqa: E402
    filter_messages,
    read_allowed_clients,
    write_manifest,
)
from build_knowledge_base import (  # noqa: E402
    DEEPSEEK_TEMPERATURE,
    MAP_THREAD_CHAR_BUDGET_MAX,
    REDUCE_GROUP_CHAR_BUDGET_MAX,
    build_knowledge_base,
    load_messages,
)

KB_DIR = ROOT / "knowledge_base" / "v1_clean"
V1_VERSION = 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Только манифест + детерминированная статистика")
    p.add_argument("--force-llm", action="store_true",
                   help="Пересобрать LLM-слой без кэша")
    p.add_argument("--workers", type=int, default=4,
                   help="Параллельных запросов к DeepSeek (по умолчанию 4)")
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    p.add_argument("--map-budget", type=int, default=MAP_THREAD_CHAR_BUDGET_MAX)
    p.add_argument("--reduce-budget", type=int, default=REDUCE_GROUP_CHAR_BUDGET_MAX)
    p.add_argument("--extract-kb", action="store_true",
                   help="После сборки извлечь movetorussia_kb.md + n8n_agent_prompt.md")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("=== v1_clean: база знаний из mailbox_export_clean ===")
    print("Загрузка очищенных переписок…")
    all_messages = load_messages(prefer_clean=True)
    allowed, flagged_n, excluded = read_allowed_clients(V1_VERSION)
    subset = filter_messages(all_messages, allowed)

    print(f"  Filter=1: {flagged_n} | отсеяно по доменам: {len(excluded)}")
    print(f"  адресов v1: {len(allowed)} | писем: {len(subset)}")

    KB_DIR.mkdir(parents=True, exist_ok=True)
    write_manifest(KB_DIR, subset)
    print(f"  манифест: {KB_DIR / 'clients_included.txt'}")
    print(
        f"  LLM: map={args.map_budget} симв., reduce={args.reduce_budget} симв., "
        f"все диалоги, workers={args.workers}"
    )

    build_knowledge_base(
        subset,
        kb_dir=KB_DIR,
        skip_llm=args.dry_run,
        max_threads=0,
        workers=args.workers,
        temperature=args.temperature,
        force_llm=args.force_llm,
        map_char_budget=args.map_budget,
        reduce_group_budget=args.reduce_budget,
        build_meta={
            "version": "v1_clean",
            "message_source": "mailbox_export_clean",
            "csv": "Clients_stats_v1.csv",
        },
    )

    if args.extract_kb and not args.dry_run:
        agent_kb = KB_DIR / "movetorussia_agent_kb.md"
        if agent_kb.is_file():
            print("Извлечение movetorussia_kb.md и n8n_agent_prompt.md…")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "extract_kb_facts.py"),
                    "--dir",
                    str(KB_DIR),
                ],
                check=False,
            )

    print(f"\nГотово: {KB_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Сборка 4 версий базы знаний MoveToRussia из размеченных заказчиком CSV.

Каждая версия — это отдельная, ни с чем не пересекающаяся база знаний,
собранная ИСКЛЮЧИТЕЛЬНО из переписок с выделенными адресами.

Версии (см. knowledge_base/clients_stats/Clients_stats_vN.csv):
  v1 — минимальная: client.Filter == 1  (+ отсев доменов-партнёров/внутренних)
  v2 — средняя:     client.Filter == 1  (+ отсев доменов)
  v3 — максимальная:client.Filter == 1  (+ отсев доменов)
  v4 — всё:         все адреса из выгрузки, без какой-либо фильтрации

Где Filter — первый столбец CSV (1 — берём, 0 — нет). В CSV v4 столбца Filter
нет — это полная статистика, поэтому v4 = вся база.

Отсев доменов (только v1–v3) — страховка на случай, если заказчик где-то
пометил Filter=1 для партнёра/внутреннего ящика по недосмотру.

Каждая версия складывается в свою папку knowledge_base/vN/:
  movetorussia_agent_kb.md  — итоговая база знаний
  analysis_stats.json       — детерминированная статистика по выборке
  clients_included.txt      — манифест включённых адресов (с числом писем)
  _intermediate/            — промежуточные извлечения LLM (кэш)

Примеры:
  python build_kb_versions.py --dry-run          # манифесты + статистика, без LLM
  python build_kb_versions.py                     # полная сборка всех версий (LLM)
  python build_kb_versions.py --only 1            # только v1
  python build_kb_versions.py --max-threads 60    # ограничить LLM топ-60 диалогов
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_knowledge_base import (  # noqa: E402
    DEEPSEEK_TEMPERATURE,
    build_knowledge_base,
    load_messages,
)

CLIENTS_STATS_DIR = ROOT / "knowledge_base" / "clients_stats"
KB_ROOT = ROOT / "knowledge_base"

# Домены, переписку с которыми исключаем из версий v1–v3:
# партнёры (бронирование, инвест-партнёры, телефония) и внутренние ящики.
EXCLUDED_DOMAINS = {
    "generalinvest.ru",
    "yesapart.com",
    "aktina.am",
    "new-tel.net",
    "siriuscapital.am",
    "cubeinvest.am",
    "uiscom.ru",
    "arkvostok.com",   # внутренняя переписка
    "movetorussia.com",  # внутренняя переписка
}

# version -> (csv filename, применять доменный фильтр, требуется столбец Filter)
VERSIONS: dict[int, dict[str, Any]] = {
    1: {"csv": "Clients_stats_v1.csv", "domain_filter": True, "has_filter": True,
        "label": "минимальная"},
    2: {"csv": "Clients_stats_v2.csv", "domain_filter": True, "has_filter": True,
        "label": "средняя"},
    3: {"csv": "Clients_stats_v3.csv", "domain_filter": True, "has_filter": True,
        "label": "максимальная"},
    4: {"csv": "Clients_stats_v4.csv", "domain_filter": False, "has_filter": False,
        "label": "всё"},
}


def domain_of(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def read_allowed_clients(version: int) -> tuple[set[str], int, set[str]]:
    """Вернуть (разрешённые адреса, число Filter=1, отсеянные по домену)."""
    cfg = VERSIONS[version]
    path = CLIENTS_STATS_DIR / cfg["csv"]
    if not path.is_file():
        raise FileNotFoundError(f"Нет CSV версии v{version}: {path}")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig"), delimiter=";"))

    flagged: set[str] = set()
    for r in rows:
        client = (r.get("client") or "").strip().lower()
        if not client:
            continue
        if cfg["has_filter"]:
            if (r.get("Filter") or "").strip() == "1":
                flagged.add(client)
        else:
            flagged.add(client)

    excluded: set[str] = set()
    if cfg["domain_filter"]:
        excluded = {c for c in flagged if domain_of(c) in EXCLUDED_DOMAINS}
    allowed = flagged - excluded
    return allowed, len(flagged), excluded


def filter_messages(
    messages: list[dict[str, Any]], allowed: set[str]
) -> list[dict[str, Any]]:
    return [m for m in messages if (m.get("counterpart") or "").lower() in allowed]


def write_manifest(kb_dir: Path, subset: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for m in subset:
        c = (m.get("counterpart") or "").lower()
        counts[c] = counts.get(c, 0) + 1
    kb_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Включённых адресов: {len(counts)} | писем: {len(subset)}", ""]
    for client, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"{cnt}\t{client}")
    (kb_dir / "clients_included.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Только манифесты + детерминированная статистика (без LLM)")
    p.add_argument("--only", type=int, choices=[1, 2, 3, 4], default=None,
                   help="Собрать только одну версию")
    p.add_argument("--max-threads", type=int, default=0,
                   help="Сколько диалогов отдавать в LLM (0 = все, макс. качество)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    p.add_argument("--force-llm", action="store_true",
                   help="Пересобрать LLM-слой без кэша")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("Загрузка всех писем…")
    all_messages = load_messages()
    print(f"  Всего писем: {len(all_messages)}\n")

    versions = [args.only] if args.only else [1, 2, 3, 4]
    for v in versions:
        cfg = VERSIONS[v]
        allowed, flagged_n, excluded = read_allowed_clients(v)
        subset = filter_messages(all_messages, allowed)
        kb_dir = KB_ROOT / f"v{v}"

        print(f"=== Версия v{v} ({cfg['label']}) ===")
        if cfg["has_filter"]:
            print(f"  Filter=1: {flagged_n}")
        else:
            print(f"  адресов в выгрузке: {flagged_n} (без фильтра)")
        if cfg["domain_filter"]:
            print(f"  отсеяно по доменам: {len(excluded)}")
        print(f"  итоговых адресов: {len(allowed)} | писем в выборке: {len(subset)}")

        write_manifest(kb_dir, subset)
        print(f"  манифест: {kb_dir / 'clients_included.txt'}")

        build_knowledge_base(
            subset,
            kb_dir=kb_dir,
            skip_llm=args.dry_run,
            max_threads=args.max_threads,
            workers=args.workers,
            temperature=args.temperature,
            force_llm=args.force_llm,
        )
        print()

    mode = "сухой прогон (без LLM)" if args.dry_run else "полная сборка"
    print(f"Готово: {mode}. Версии: {', '.join('v'+str(v) for v in versions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

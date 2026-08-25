"""
FAQ-справочник v4 — полностью через DeepSeek LLM.

Вместо эвристического extract + Jaccard-кластеризации:
  1) Map: для каждого двустороннего диалога LLM извлекает пары «вопрос клиента → ответ менеджера»
     (ответ должен реально отвечать на вопрос, без шаблонов и scheduling).
  2) Reduce: LLM объединяет похожие вопросы, считает frequency, формирует канонический Q+A на EN.
  3) CSV для заказчика.

Вход:  mailbox_export_clean/threads/*.txt + Clients_stats_v4.csv
Выход: knowledge_base/v4/client_faq_review.csv (+ frequent/full/stats)
Кэш:   knowledge_base/v4/_faq_intermediate/llm_pairs/, llm_merge/, llm_canonical.json

Примеры:
  python build_faq_llm.py --max-threads 5          # быстрый тест
  python build_faq_llm.py                          # все двусторонние диалоги
  python build_faq_llm.py --force                  # пересобрать extract
  python build_faq_llm.py --force-merge            # только reduce (extract из кэша)
  python build_faq_llm.py --semantic-only          # умное слияние похожих FAQ (DeepSeek)
  python build_faq_llm.py --semantic-only --force-semantic
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_faq_catalog import THEME_RULES  # noqa: E402
from build_kb_versions import filter_messages, read_allowed_clients, write_manifest  # noqa: E402
from build_knowledge_base import (  # noqa: E402
    DEEPSEEK_TEMPERATURE,
    _render_thread,
    call_deepseek,
    group_threads,
    load_messages,
)

KB_V4 = ROOT / "knowledge_base" / "v4"
INTERMEDIATE = KB_V4 / "_faq_intermediate"
LLM_PAIRS_DIR = INTERMEDIATE / "llm_pairs"
LLM_MERGE_DIR = INTERMEDIATE / "llm_merge"
LLM_RAW_PATH = INTERMEDIATE / "llm_raw_pairs.jsonl"
LLM_CANONICAL_PATH = INTERMEDIATE / "llm_canonical.json"
LLM_SEMANTIC_DIR = INTERMEDIATE / "llm_semantic"
LLM_SEMANTIC_PATH = INTERMEDIATE / "llm_semantic.json"
LLM_SEMANTIC_STATE = INTERMEDIATE / "llm_semantic_state.json"
LLM_EXTRACT_MANIFEST = INTERMEDIATE / "llm_extract_manifest.json"

MAX_SEMANTIC_REDUCE_DEPTH = 2
STATS_PATH = KB_V4 / "faq_llm_stats.json"
CSV_PATH = KB_V4 / "client_faq.csv"
CSV_FREQUENT_PATH = KB_V4 / "client_faq_frequent.csv"
CSV_REVIEW_PATH = KB_V4 / "client_faq_review.csv"

THEME_LABELS: dict[str, str] = {k: label for k, label, _ in THEME_RULES}
THEME_LABELS.setdefault("other", "Other")

VALID_THEMES = set(THEME_LABELS) | {"other"}

EXTRACT_SYSTEM = """You extract FAQ entries from MoveToRussia client-manager email threads.
MoveToRussia helps citizens of unfriendly countries relocate to Russia (TRP, Golden Visa, etc.).

For each genuine CLIENT question that received a substantive MANAGER answer in THIS thread, output one entry:
- question: standalone question in English (translate if needed)
- answer: manager's factual answer in English, anonymized (no names, no signatures, no "Dear...")
  Include ONLY the sentences that directly answer THIS question.
- theme: one of bank_transfer, trp_process, golden_visa, documents, cost_pricing, hotel_registration,
  visa_entry, family, timeline, language_test, contract, citizenship, work_sim, tax, medical,
  driver_license, other

STRICT RULES:
- Skip manager questions, scheduling, greetings, "anything else?", call invitations without substance.
- answer MUST directly address question. If manager sent one long email covering multiple topics,
  split into separate entries with the relevant excerpt for each question.
- Do NOT pair a question with boilerplate, call scheduling, or text answering a different question.
- Do NOT invent facts, links, prices, or guarantees not in the thread.
- Return JSON array ONLY: [{"question":"...","answer":"...","theme":"..."}]
- If no valid pairs, return []"""

EXTRACT_USER = """Thread with client {client}:

{thread}

Return JSON array only."""

MERGE_SYSTEM = """You build a deduplicated FAQ catalog for MoveToRussia managers from raw Q→A pairs
extracted from many client threads. Output in English.

Input: numbered raw pairs (may contain duplicates and near-duplicates).

Output JSON array:
[{"question":"...","answer":"...","theme":"...","frequency": N, "variants": ["alt phrasing", ...]}]

RULES:
- Merge semantically identical or near-identical questions; frequency = count of merged source pairs.
- Pick or synthesize the clearest question and the most complete accurate answer from inputs.
- answer MUST match question — if a source pair has mismatched Q/A, DROP it, do not merge it in.
- Drop scheduling-only, greeting-only, and fragment entries with no substantive answer.
- Keep rare but substantive questions (frequency=1 is OK).
- Anonymize: no client/manager names, no email signatures.
- Do NOT invent facts.
- Return JSON array only."""

MERGE_USER = """Merge these raw FAQ pairs into canonical entries:

{pairs}

Return JSON array only."""

FINAL_SYSTEM = """You finalize a MoveToRussia FAQ catalog by merging overlapping entries from batch merges.
Same output format and rules as merge step. Remove duplicates, fix any Q/A mismatches, keep all substantive topics.
Return JSON array only."""

SEMANTIC_MERGE_SYSTEM = """You deduplicate MoveToRussia FAQ entries by CLIENT INTENT (what the client wants to know).

Many entries ask the SAME thing in different words — merge them into one canonical entry.

MUST MERGE (same intent):
- "How can I transfer money to Russia?" + "Is there any way to transfer my savings to Russia?"
- "How can I relocate my savings to Russia?" + "Can we transfer our pension from Belgium to Russia?"
- "How to wire funds for property purchase?" + "How to send monthly income to a Russian bank?"

DO NOT MERGE (different intents):
- "Transfer savings to Russia" vs "What interest rates on Russian savings accounts?"
- "Wire money to Russia" vs "How to convert/unfreeze Type C brokerage account?"
- "Minimum savings for relocation" vs "How to physically transfer capital"

Input: numbered FAQ rows (question, answer, frequency, theme).

Output JSON array:
[{"question":"...","answer":"...","theme":"...","frequency": N, "variants": ["alt phrasing", ...]}]

RULES:
- Merge same underlying intent even if wording differs a lot.
- frequency = sum of merged rows' frequencies.
- Pick clearest question; synthesize the most complete accurate answer from all inputs.
- variants = other question wordings (include prior variants).
- Do NOT invent facts. Drop rows that are not real FAQ.
- Return JSON array only."""

SEMANTIC_MERGE_USER = """Merge these FAQ entries by client intent:

{entries}

Return JSON array only."""

# Тематические «корзины» — собираем похожие вопросы из разных theme/frequency для LLM-слияния.
INTENT_BUCKETS: list[tuple[str, re.Pattern[str]]] = [
    ("money_to_russia", re.compile(
        r"(?:transfer|send|move|relocate|bring|wire|remit|convert|payment).{0,50}"
        r"(?:savings?|money|fund|pension|capital|income|amount|sum)s?|"
        r"(?:savings?|pension|fund|capital|income).{0,50}"
        r"(?:transfer|send|move|relocate|russia)|"
        r"financial infrastructure|brokerage.{0,30}russia|"
        r"(?:large|monthly).{0,25}(?:sum|amount|payment|income).{0,25}russia",
        re.I,
    )),
    ("type_c_frozen", re.compile(
        r"type\s*c|frozen.{0,20}(?:account|fund|stock|dividend)|unblock.{0,20}account",
        re.I,
    )),
    ("svv_trp_process", re.compile(
        r"shared values|temporary residence|\btrp\b|residence permit.{0,30}(?:process|obtain|apply|timeline)",
        re.I,
    )),
    ("white_glove_pricing", re.compile(
        r"white glove|service (?:fee|cost|package|pricing)|how much.{0,30}(?:service|support|assist)",
        re.I,
    )),
    ("golden_visa_requirements", re.compile(
        r"golden visa.{0,40}(?:require|invest|property|deposit|eligible)|"
        r"invest.{0,30}(?:golden visa|permanent residence)",
        re.I,
    )),
    ("entry_visa_travel", re.compile(
        r"(?:tourist|entry|e-?).{0,10}visa|fly(?:ing)? from|flight.{0,20}(?:us|uk|europe)",
        re.I,
    )),
]

FINAL_USER = """Finalize this FAQ catalog (may still have duplicates):

{entries}

Return JSON array only."""


@dataclass
class RawPair:
    question: str
    answer: str
    theme: str
    client: str


@dataclass
class FaqEntry:
    question: str
    answer: str
    theme: str
    theme_label: str
    frequency: int
    variants: list[str] = field(default_factory=list)


def _anon_client(client: str) -> str:
    if "@" in client:
        local, domain = client.rsplit("@", 1)
        return f"client@{domain}"
    return "client"


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("ожидался JSON-массив")
    return data


def _normalize_theme(theme: str) -> str:
    t = (theme or "other").strip().lower().replace(" ", "_")
    return t if t in VALID_THEMES else "other"


def _clean_pair_row(row: dict[str, Any]) -> dict[str, str] | None:
    q = (row.get("question") or "").strip()
    a = (row.get("answer") or "").strip()
    if len(q) < 12 or len(a) < 40:
        return None
    if not q.endswith("?"):
        q = q.rstrip(".") + "?"
    return {
        "question": q,
        "answer": a,
        "theme": _normalize_theme(str(row.get("theme", "other"))),
    }


def _extract_one(
    idx: int,
    total: int,
    client: str,
    msgs: list[dict[str, Any]],
    *,
    temperature: float,
    char_budget: int,
    force: bool,
    log_lock: Lock,
) -> tuple[int, list[RawPair]]:
    cache = LLM_PAIRS_DIR / f"{idx:04d}.json"
    if cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            pairs = [
                RawPair(
                    question=p["question"],
                    answer=p["answer"],
                    theme=p.get("theme", "other"),
                    client=client,
                )
                for p in data.get("pairs", [])
            ]
            with log_lock:
                print(f"  Extract {idx}/{total} (кэш, {len(pairs)} пар)")
            return idx, pairs
        except Exception:
            pass

    thread_text = _render_thread(client, msgs, char_budget)
    user = EXTRACT_USER.format(client=_anon_client(client), thread=thread_text)
    try:
        raw = call_deepseek(EXTRACT_SYSTEM, user, temperature=temperature)
        rows = _parse_json_array(raw)
    except Exception as exc:
        with log_lock:
            print(f"  Extract {idx}/{total} ошибка: {exc}", file=sys.stderr)
        rows = []

    pairs: list[RawPair] = []
    for row in rows:
        cleaned = _clean_pair_row(row)
        if cleaned:
            pairs.append(RawPair(client=client, **cleaned))

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "client": _anon_client(client),
                "pairs": [
                    {"question": p.question, "answer": p.answer, "theme": p.theme}
                    for p in pairs
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with log_lock:
        print(f"  Extract {idx}/{total} готово ({len(pairs)} пар)")
    return idx, pairs


def run_extract(
    threads: dict[str, list[dict[str, Any]]],
    *,
    max_threads: int,
    temperature: float,
    workers: int,
    char_budget: int,
    force: bool,
) -> list[RawPair]:
    two_way = {
        c: ms
        for c, ms in threads.items()
        if any(x["direction"] == "outgoing" for x in ms)
        and any(x["direction"] == "incoming" for x in ms)
    }
    ranked = sorted(two_way.items(), key=lambda kv: len(kv[1]), reverse=True)
    if max_threads and max_threads > 0:
        ranked = ranked[:max_threads]

    total = len(ranked)
    print(
        f"  Extract: диалогов {total} (из {len(two_way)} двусторонних), "
        f"бюджет {char_budget} симв., workers={workers}"
    )
    if not total:
        return []

    log_lock = Lock()
    results: dict[int, list[RawPair]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _extract_one,
                i,
                total,
                client,
                msgs,
                temperature=temperature,
                char_budget=char_budget,
                force=force,
                log_lock=log_lock,
            ): i
            for i, (client, msgs) in enumerate(ranked, start=1)
        }
        for fut in as_completed(futures):
            idx, pairs = fut.result()
            results[idx] = pairs

    all_pairs: list[RawPair] = []
    for i in range(1, total + 1):
        all_pairs.extend(results.get(i, []))

    LLM_RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LLM_RAW_PATH.open("w", encoding="utf-8") as f:
        for p in all_pairs:
            f.write(
                json.dumps(
                    {
                        "question": p.question,
                        "answer": p.answer,
                        "theme": p.theme,
                        "client": _anon_client(p.client),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    _write_extract_manifest(
        threads_target=total,
        max_threads=max_threads,
        two_way_total=len(two_way),
        raw_pairs_count=len(all_pairs),
        extract_complete=True,
    )
    return all_pairs


def _extract_target_count(two_way_n: int, max_threads: int) -> int:
    if max_threads and max_threads > 0:
        return min(two_way_n, max_threads)
    return two_way_n


def _read_extract_manifest() -> dict[str, Any]:
    if not LLM_EXTRACT_MANIFEST.is_file():
        return {}
    try:
        return json.loads(LLM_EXTRACT_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_extract_manifest(**fields: Any) -> None:
    data = _read_extract_manifest()
    if "raw_pairs_count" in fields:
        merged_for = data.get("merged_for_raw_pairs_count")
        if merged_for is not None and fields["raw_pairs_count"] != merged_for:
            fields.setdefault("merge_complete", False)
    data.update(fields)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    LLM_EXTRACT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    LLM_EXTRACT_MANIFEST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_cache_valid(raw_pairs_count: int) -> bool:
    manifest = _read_extract_manifest()
    if not manifest.get("merge_complete"):
        return False
    if not LLM_CANONICAL_PATH.is_file():
        return False
    merged_for = manifest.get("merged_for_raw_pairs_count")
    if merged_for is not None:
        return merged_for == raw_pairs_count
    if manifest.get("raw_pairs_count") != raw_pairs_count:
        return False
    canon_n = int(manifest.get("canonical_entries") or 0)
    if raw_pairs_count > 50 and canon_n < max(10, raw_pairs_count // 25):
        return False
    return True


def _cached_thread_files() -> int:
    if not LLM_PAIRS_DIR.is_dir():
        return 0
    return len(list(LLM_PAIRS_DIR.glob("*.json")))


def _extract_is_complete(two_way_n: int, max_threads: int) -> bool:
    manifest = _read_extract_manifest()
    if not manifest.get("extract_complete"):
        return False
    target = _extract_target_count(two_way_n, max_threads)
    if manifest.get("threads_target") != target:
        return False
    if manifest.get("max_threads", 0) != max_threads:
        return False
    return _cached_thread_files() >= target


def _merge_batch(
    batch_idx: int,
    pairs: list[RawPair],
    *,
    temperature: float,
    force: bool,
    pass_name: str,
) -> list[FaqEntry]:
    cache = LLM_MERGE_DIR / f"{pass_name}_{batch_idx:03d}.json"
    if cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return [_row_to_entry(r) for r in data]
        except Exception:
            pass

    lines: list[str] = []
    for i, p in enumerate(pairs, start=1):
        lines.append(
            f"#{i}\nQ: {p.question}\nA: {p.answer[:1200]}\nTheme: {p.theme}\n"
        )
    user = MERGE_USER.format(pairs="\n".join(lines))
    raw = call_deepseek(MERGE_SYSTEM, user, temperature=temperature)
    rows = _parse_json_array(raw)
    entries = [_row_to_entry(r) for r in rows if _row_to_entry(r)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            [
                {
                    "question": e.question,
                    "answer": e.answer,
                    "theme": e.theme,
                    "frequency": e.frequency,
                    "variants": e.variants,
                }
                for e in entries
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return entries


def _row_to_entry(row: dict[str, Any]) -> FaqEntry | None:
    q = (row.get("question") or "").strip()
    a = (row.get("answer") or "").strip()
    if len(q) < 12 or len(a) < 40:
        return None
    theme = _normalize_theme(str(row.get("theme", "other")))
    freq = int(row.get("frequency") or 1)
    variants = row.get("variants") or []
    if isinstance(variants, str):
        variants = [v.strip() for v in variants.split("|") if v.strip()]
    return FaqEntry(
        question=q if q.endswith("?") else q.rstrip(".") + "?",
        answer=a,
        theme=theme,
        theme_label=THEME_LABELS.get(theme, "Other"),
        frequency=max(1, freq),
        variants=[str(v) for v in variants][:8],
    )


def _entries_to_raw(entries: list[FaqEntry]) -> list[RawPair]:
    out: list[RawPair] = []
    for e in entries:
        out.append(
            RawPair(
                question=e.question,
                answer=e.answer,
                theme=e.theme,
                client="merged",
            )
        )
        for v in e.variants:
            if v and v != e.question:
                out.append(RawPair(question=v, answer=e.answer, theme=e.theme, client="variant"))
    return out


def _entry_search_text(entry: FaqEntry) -> str:
    return f"{entry.question} {' '.join(entry.variants)} {entry.answer[:300]}"


def _entries_from_json_rows(rows: list[dict[str, Any]]) -> list[FaqEntry]:
    out: list[FaqEntry] = []
    for row in rows:
        e = _row_to_entry(row)
        if e:
            out.append(e)
    return out


def _load_canonical_entries() -> list[FaqEntry]:
    if not LLM_CANONICAL_PATH.is_file():
        raise FileNotFoundError(f"Нет {LLM_CANONICAL_PATH}. Сначала запустите merge.")
    rows = json.loads(LLM_CANONICAL_PATH.read_text(encoding="utf-8"))
    return _entries_from_json_rows(rows)


def _save_semantic_entries(entries: list[FaqEntry], source_count: int) -> None:
    LLM_SEMANTIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    LLM_SEMANTIC_PATH.write_text(
        json.dumps(
            [
                {
                    "question": e.question,
                    "answer": e.answer,
                    "theme": e.theme,
                    "theme_label": e.theme_label,
                    "frequency": e.frequency,
                    "variants": e.variants,
                }
                for e in entries
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_extract_manifest(
        semantic_complete=True,
        semantic_for_count=source_count,
        semantic_entries=len(entries),
    )


def _save_semantic_state(
    entries: list[FaqEntry],
    completed_buckets: set[str],
    source_count: int,
) -> None:
    LLM_SEMANTIC_STATE.parent.mkdir(parents=True, exist_ok=True)
    LLM_SEMANTIC_STATE.write_text(
        json.dumps(
            {
                "source_count": source_count,
                "completed_buckets": sorted(completed_buckets),
                "entries": [
                    {
                        "question": e.question,
                        "answer": e.answer,
                        "theme": e.theme,
                        "theme_label": e.theme_label,
                        "frequency": e.frequency,
                        "variants": e.variants,
                    }
                    for e in entries
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_semantic_state(source_count: int) -> tuple[list[FaqEntry], set[str]] | None:
    if not LLM_SEMANTIC_STATE.is_file():
        return None
    try:
        data = json.loads(LLM_SEMANTIC_STATE.read_text(encoding="utf-8"))
        if data.get("source_count") != source_count:
            return None
        entries = _entries_from_json_rows(data.get("entries", []))
        completed = set(data.get("completed_buckets", []))
        return entries, completed
    except Exception:
        return None


def _semantic_cache_valid(source_count: int) -> bool:
    manifest = _read_extract_manifest()
    if not manifest.get("semantic_complete"):
        return False
    if manifest.get("semantic_for_count") != source_count:
        return False
    return LLM_SEMANTIC_PATH.is_file()


def _semantic_merge_batch(
    entries: list[FaqEntry],
    *,
    cache_key: str,
    temperature: float,
    force: bool,
) -> list[FaqEntry]:
    cache = LLM_SEMANTIC_DIR / f"{cache_key}.json"
    if cache.is_file() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            merged = _entries_from_json_rows(data)
            print(f"        (кэш {cache_key}, {len(merged)} записей)", flush=True)
            return merged
        except Exception:
            pass

    lines: list[str] = []
    for i, e in enumerate(entries, start=1):
        variants = " | ".join(e.variants[:4])
        lines.append(
            f"#{i} [freq={e.frequency}] theme={e.theme}\n"
            f"Q: {e.question}\n"
            f"A: {e.answer[:800]}\n"
            f"Variants: {variants}\n"
        )
    user = SEMANTIC_MERGE_USER.format(entries="\n".join(lines))
    t0 = time.time()
    raw = call_deepseek(SEMANTIC_MERGE_SYSTEM, user, temperature=temperature)
    merged = _entries_from_json_rows(_parse_json_array(raw))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            [
                {
                    "question": e.question,
                    "answer": e.answer,
                    "theme": e.theme,
                    "frequency": e.frequency,
                    "variants": e.variants,
                }
                for e in merged
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"        API {cache_key}: {len(entries)} → {len(merged)} ({time.time() - t0:.0f} с)", flush=True)
    return merged


def _semantic_merge_pool(
    pool: list[FaqEntry],
    *,
    label: str,
    temperature: float,
    batch_size: int,
    force: bool,
    workers: int = 4,
    depth: int = 0,
) -> list[FaqEntry]:
    if len(pool) <= 1:
        return pool
    if len(pool) <= batch_size:
        return _semantic_merge_batch(
            pool,
            cache_key=f"{label}_all",
            temperature=temperature,
            force=force,
        )

    batches = [pool[i : i + batch_size] for i in range(0, len(pool), batch_size)]
    total = len(batches)
    merged: list[FaqEntry] = []

    if workers > 1 and total > 1:
        results: dict[int, list[FaqEntry]] = {}
        with ThreadPoolExecutor(max_workers=min(workers, total)) as ex:
            futures = {
                ex.submit(
                    _semantic_merge_batch,
                    batch,
                    cache_key=f"{label}_{bi:02d}",
                    temperature=temperature,
                    force=force,
                ): bi
                for bi, batch in enumerate(batches, start=1)
            }
            done = 0
            for fut in as_completed(futures):
                bi = futures[fut]
                results[bi] = fut.result()
                done += 1
                print(f"      {label}: {done}/{total} батчей", flush=True)
        for bi in range(1, total + 1):
            merged.extend(results.get(bi, []))
    else:
        for bi, batch in enumerate(batches, start=1):
            print(f"      {label}: batch {bi}/{total}…", flush=True)
            merged.extend(
                _semantic_merge_batch(
                    batch,
                    cache_key=f"{label}_{bi:02d}",
                    temperature=temperature,
                    force=force,
                )
            )

    if len(merged) < len(pool) and depth < MAX_SEMANTIC_REDUCE_DEPTH:
        print(
            f"      {label}: reduce {len(pool)} → {len(merged)} "
            f"(глубина {depth + 1}/{MAX_SEMANTIC_REDUCE_DEPTH})…",
            flush=True,
        )
        return _semantic_merge_pool(
            merged,
            label=f"{label}_reduce",
            temperature=temperature,
            batch_size=batch_size,
            force=force,
            workers=workers,
            depth=depth + 1,
        )
    return merged


def _replace_entries(
    current: list[FaqEntry],
    remove: list[FaqEntry],
    add: list[FaqEntry],
) -> list[FaqEntry]:
    remove_q = {e.question for e in remove}
    kept = [e for e in current if e.question not in remove_q]
    kept.extend(add)
    return kept


def _semantic_pass_by_theme(
    entries: list[FaqEntry],
    *,
    pass_num: int,
    temperature: float,
    batch_size: int,
    force: bool,
    workers: int,
) -> list[FaqEntry]:
    by_theme: dict[str, list[FaqEntry]] = defaultdict(list)
    for e in entries:
        by_theme[e.theme].append(e)

    result: list[FaqEntry] = []
    for theme, group in sorted(by_theme.items()):
        if len(group) < 3:
            result.extend(group)
            continue
        print(f"    theme {theme}: {len(group)} записей…", flush=True)
        merged = _semantic_merge_pool(
            group,
            label=f"theme_{theme}_p{pass_num}",
            temperature=temperature,
            batch_size=batch_size,
            force=force,
            workers=workers,
        )
        result.extend(merged)
    return result


def run_semantic_merge(
    entries: list[FaqEntry],
    *,
    temperature: float,
    batch_size: int,
    force: bool,
    workers: int = 4,
) -> list[FaqEntry]:
    source_count = len(entries)
    if source_count <= 1:
        return entries

    print(
        f"  Semantic: {source_count} записей, batch={batch_size}, "
        f"workers={workers}, max_reduce={MAX_SEMANTIC_REDUCE_DEPTH}",
        flush=True,
    )

    completed_buckets: set[str] = set()
    if not force:
        state = _load_semantic_state(source_count)
        if state:
            current, completed_buckets = state
            print(
                f"  Resume: {len(current)} записей, "
                f"готово buckets: {', '.join(sorted(completed_buckets)) or '—'}",
                flush=True,
            )
        else:
            current = list(entries)
    else:
        current = list(entries)
        if LLM_SEMANTIC_STATE.is_file():
            LLM_SEMANTIC_STATE.unlink(missing_ok=True)

    assigned: set[str] = set()
    for e in current:
        for bucket_name, pattern in INTENT_BUCKETS:
            if bucket_name in completed_buckets and pattern.search(_entry_search_text(e)):
                assigned.add(e.question)

    for bucket_name, pattern in INTENT_BUCKETS:
        if bucket_name in completed_buckets:
            print(f"  Bucket «{bucket_name}»: пропуск (готово)", flush=True)
            continue
        bucket = [
            e for e in current
            if e.question not in assigned and pattern.search(_entry_search_text(e))
        ]
        if len(bucket) < 2:
            completed_buckets.add(bucket_name)
            if not force:
                _save_semantic_state(current, completed_buckets, source_count)
            continue
        print(f"  Bucket «{bucket_name}»: {len(bucket)} похожих → DeepSeek…", flush=True)
        t0 = time.time()
        merged = _semantic_merge_pool(
            bucket,
            label=f"bucket_{bucket_name}",
            temperature=temperature,
            batch_size=batch_size,
            force=force,
            workers=workers,
        )
        current = _replace_entries(current, bucket, merged)
        assigned.update(e.question for e in bucket)
        completed_buckets.add(bucket_name)
        if not force:
            _save_semantic_state(current, completed_buckets, source_count)
        print(f"    → {len(merged)} записей (всего {len(current)}, {time.time() - t0:.0f} с)", flush=True)

    for pass_num in range(1, 3):
        prev = len(current)
        if prev < 100:
            break
        print(f"  Semantic theme pass {pass_num}…", flush=True)
        t0 = time.time()
        current = _semantic_pass_by_theme(
            current,
            pass_num=pass_num,
            temperature=temperature,
            batch_size=batch_size,
            force=force,
            workers=workers,
        )
        print(f"    {prev} → {len(current)} ({time.time() - t0:.0f} с)", flush=True)
        if len(current) >= prev * 0.97:
            break

    current.sort(key=lambda e: (-e.frequency, e.theme, e.question))
    _save_semantic_entries(current, source_count)
    if LLM_SEMANTIC_STATE.is_file():
        LLM_SEMANTIC_STATE.unlink(missing_ok=True)
    return current


def run_merge(
    pairs: list[RawPair],
    *,
    temperature: float,
    batch_size: int,
    force: bool,
) -> list[FaqEntry]:
    raw_pairs_count = len(pairs)
    if not pairs:
        return []

    print(f"  Merge pass 1: {len(pairs)} сырых пар, batch={batch_size}")
    batches = [pairs[i : i + batch_size] for i in range(0, len(pairs), batch_size)]
    merged: list[FaqEntry] = []
    for bi, batch in enumerate(batches, start=1):
        print(f"    batch {bi}/{len(batches)} ({len(batch)} пар)…")
        t0 = time.time()
        merged.extend(
            _merge_batch(bi, batch, temperature=temperature, force=force, pass_name="merge1")
        )
        print(f"      → {len(merged)} канонических (за {time.time() - t0:.0f} с)")

    # Повторный reduce, если много записей
    pass_num = 2
    while len(merged) > 80 and len(merged) > batch_size:
        print(f"  Merge pass {pass_num}: {len(merged)} записей → reduce…")
        raw = _entries_to_raw(merged)
        batches = [raw[i : i + batch_size] for i in range(0, len(raw), batch_size)]
        next_merged: list[FaqEntry] = []
        for bi, batch in enumerate(batches, start=1):
            print(f"    reduce batch {bi}/{len(batches)}…")
            next_merged.extend(
                _merge_batch(
                    bi,
                    batch,
                    temperature=temperature,
                    force=force,
                    pass_name=f"merge{pass_num}",
                )
            )
        if len(next_merged) >= len(merged):
            break
        merged = next_merged
        pass_num += 1

    if len(merged) > 1:
        print(f"  Final merge: {len(merged)} записей…")
        lines = []
        for i, e in enumerate(merged, start=1):
            lines.append(
                f"#{i} (freq={e.frequency})\nQ: {e.question}\nA: {e.answer[:1000]}\n"
                f"Theme: {e.theme}\nVariants: {' | '.join(e.variants[:3])}\n"
            )
        user = FINAL_USER.format(entries="\n".join(lines))
        raw = call_deepseek(FINAL_SYSTEM, user, temperature=temperature)
        final_rows = _parse_json_array(raw)
        final = [_row_to_entry(r) for r in final_rows]
        final = [e for e in final if e]
        if final:
            merged = final

    merged.sort(key=lambda e: (-e.frequency, e.theme, e.question))
    LLM_CANONICAL_PATH.write_text(
        json.dumps(
            [
                {
                    "question": e.question,
                    "answer": e.answer,
                    "theme": e.theme,
                    "theme_label": e.theme_label,
                    "frequency": e.frequency,
                    "variants": e.variants,
                }
                for e in merged
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_extract_manifest(
        merge_complete=True,
        canonical_entries=len(merged),
        merged_for_raw_pairs_count=raw_pairs_count,
    )
    return merged


def write_csv(entries: list[FaqEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow([
            "number", "theme", "theme_label", "frequency", "languages",
            "question", "question_original", "answer", "variants",
        ])
        for i, e in enumerate(entries, start=1):
            w.writerow([
                i,
                e.theme,
                e.theme_label,
                e.frequency,
                "en",
                e.question,
                "",
                e.answer,
                " | ".join(e.variants[:5]),
            ])


def write_stats(
    *,
    threads_total: int,
    threads_processed: int,
    raw_pairs: int,
    canonical: list[FaqEntry],
    allowed_clients: int,
    messages: int,
    semantic_count: int | None = None,
) -> None:
    freq_hist = Counter(e.frequency for e in canonical)
    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline": "llm",
        "clients_in_scope": allowed_clients,
        "messages_in_scope": messages,
        "two_way_threads": threads_total,
        "threads_processed": threads_processed,
        "raw_pairs_extracted": raw_pairs,
        "canonical_entries": len(canonical),
        "semantic_entries": semantic_count,
        "frequency_distribution": dict(sorted(freq_hist.items())),
        "outputs": {
            "csv_full": str(CSV_PATH.relative_to(ROOT)),
            "csv_frequent": str(CSV_FREQUENT_PATH.relative_to(ROOT)),
            "csv_review": str(CSV_REVIEW_PATH.relative_to(ROOT)),
            "intermediate": str(INTERMEDIATE.relative_to(ROOT)),
        },
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jsonl", action="store_true",
                   help="Брать письма из mailbox_export/all_messages.jsonl")
    p.add_argument("--force", action="store_true", help="Пересобрать extract (llm_pairs/)")
    p.add_argument("--force-merge", action="store_true", help="Пересобрать merge/reduce")
    p.add_argument("--merge-only", action="store_true",
                   help="Только merge из llm_raw_pairs.jsonl / llm_pairs/")
    p.add_argument("--semantic-only", action="store_true",
                   help="Только semantic merge из llm_canonical.json → CSV")
    p.add_argument("--skip-semantic", action="store_true",
                   help="Не запускать semantic merge после обычного merge")
    p.add_argument("--force-semantic", action="store_true",
                   help="Пересобрать semantic merge")
    p.add_argument("--semantic-batch-size", type=int, default=20,
                   help="Размер батча для semantic merge")
    p.add_argument("--semantic-workers", type=int, default=4,
                   help="Параллельных запросов semantic merge")
    p.add_argument("--max-threads", type=int, default=0,
                   help="Лимит диалогов для extract (0 = все двусторонние)")
    p.add_argument("--workers", type=int, default=4, help="Параллельных запросов extract")
    p.add_argument("--char-budget", type=int, default=14000,
                   help="Символов на диалог для LLM")
    p.add_argument("--merge-batch-size", type=int, default=25)
    p.add_argument("--min-frequency", type=int, default=2,
                   help="Порог для client_faq_frequent.csv")
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    return p.parse_args()


def _load_raw_pairs_from_cache() -> list[RawPair]:
    if LLM_RAW_PATH.is_file():
        pairs: list[RawPair] = []
        for line in LLM_RAW_PATH.open(encoding="utf-8"):
            row = json.loads(line)
            pairs.append(
                RawPair(
                    question=row["question"],
                    answer=row["answer"],
                    theme=row.get("theme", "other"),
                    client=row.get("client", "cached"),
                )
            )
        return pairs
    pairs: list[RawPair] = []
    for path in sorted(LLM_PAIRS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        client = data.get("client", "cached")
        for row in data.get("pairs", []):
            pairs.append(
                RawPair(
                    question=row["question"],
                    answer=row["answer"],
                    theme=row.get("theme", "other"),
                    client=client,
                )
            )
    return pairs


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("=== FAQ LLM-пайплайн v4 (DeepSeek) ===")
    KB_V4.mkdir(parents=True, exist_ok=True)

    manifest = _read_extract_manifest()
    if args.semantic_only or args.merge_only:
        allowed_n = manifest.get("clients_in_scope", 0)
        messages_n = manifest.get("messages_in_scope", 0)
        two_way_n = manifest.get("two_way_total", manifest.get("threads_target", 0))
        threads_processed = manifest.get("threads_target", two_way_n)
        mode = "semantic-only" if args.semantic_only else "merge-only"
        print(
            f"  {mode} (без перечитывания почты) | "
            f"диалогов в extract: {threads_processed} | "
            f"сырых пар: {manifest.get('raw_pairs_count', '?')}"
        )
    else:
        all_messages = load_messages(prefer_clean=not args.jsonl)
        allowed, _, _ = read_allowed_clients(4)
        messages = filter_messages(all_messages, allowed)
        threads = group_threads(messages)
        two_way_n = sum(
            1
            for ms in threads.values()
            if any(x["direction"] == "outgoing" for x in ms)
            and any(x["direction"] == "incoming" for x in ms)
        )
        allowed_n = len(allowed)
        messages_n = len(messages)
        print(f"  адресов: {allowed_n} | писем: {messages_n} | двусторонних диалогов: {two_way_n}")
        write_manifest(KB_V4, messages)
        _write_extract_manifest(
            clients_in_scope=allowed_n,
            messages_in_scope=messages_n,
        )

    raw_pairs: list[RawPair] = []
    canonical: list[FaqEntry] = []

    if args.semantic_only:
        canonical = _load_canonical_entries()
        print(f"  canonical (до semantic): {len(canonical)} записей")
    else:
        threads_target = _extract_target_count(two_way_n, args.max_threads)
        if not args.merge_only:
            threads_processed = threads_target
        extract_complete = _extract_is_complete(two_way_n, args.max_threads) and not args.force

        if args.merge_only:
            print("Merge-only: загрузка сырых пар из кэша…")
            raw_pairs = _load_raw_pairs_from_cache()
            print(f"  сырых пар: {len(raw_pairs)}")
        elif extract_complete:
            raw_pairs = _load_raw_pairs_from_cache()
            print(
                f"Extract: из кэша ({threads_target} диалогов, {len(raw_pairs)} пар). "
                f"--force для пересборки."
            )
        else:
            cached_n = _cached_thread_files()
            if cached_n and not args.force:
                print(
                    f"Extract: продолжение ({cached_n}/{threads_target} диалогов в кэше), "
                    f"workers={args.workers}…"
                )
            else:
                print(f"Extract: DeepSeek по {threads_target} диалогам, workers={args.workers}…")
            t0 = time.time()
            raw_pairs = run_extract(
                threads,
                max_threads=args.max_threads,
                temperature=args.temperature,
                workers=args.workers,
                char_budget=args.char_budget,
                force=args.force,
            )
            print(f"  сырых пар: {len(raw_pairs)} ({time.time() - t0:.0f} с)")
            _write_extract_manifest(
                clients_in_scope=allowed_n,
                messages_in_scope=messages_n,
            )

        merge_from_cache = (
            _merge_cache_valid(len(raw_pairs))
            and not args.force_merge
            and not args.force
            and not args.merge_only
        )
        if merge_from_cache:
            print(
                f"Merge: из кэша ({len(raw_pairs)} пар → "
                f"{manifest.get('canonical_entries', '?')} записей). "
                f"--force-merge для пересборки."
            )
            canonical = _load_canonical_entries()
        else:
            if manifest.get("merge_complete") and not _merge_cache_valid(len(raw_pairs)):
                print(
                    f"Merge: кэш устарел "
                    f"({manifest.get('canonical_entries', '?')} записей для "
                    f"{manifest.get('merged_for_raw_pairs_count') or manifest.get('raw_pairs_count', '?')} пар, "
                    f"сейчас {len(raw_pairs)} пар)."
                )
            print("Merge/Reduce: DeepSeek…")
            t0 = time.time()
            canonical = run_merge(
                raw_pairs,
                temperature=args.temperature,
                batch_size=args.merge_batch_size,
                force=True,
            )
            _write_extract_manifest(raw_pairs_count=len(raw_pairs))
            print(f"  канонических записей: {len(canonical)} ({time.time() - t0:.0f} с)")

    merge_input = canonical if canonical else _load_canonical_entries()
    output_entries = merge_input

    if not args.skip_semantic:
        semantic_from_cache = (
            _semantic_cache_valid(len(merge_input))
            and not args.force_semantic
            and not args.force
            and not args.force_merge
            and not args.semantic_only
        )
        if semantic_from_cache:
            print(
                f"Semantic: из кэша ({len(merge_input)} → "
                f"{manifest.get('semantic_entries', '?')} записей). "
                f"--force-semantic для пересборки."
            )
            output_entries = _entries_from_json_rows(
                json.loads(LLM_SEMANTIC_PATH.read_text(encoding="utf-8"))
            )
        else:
            if args.semantic_only:
                print("Semantic merge: DeepSeek (похожие вопросы по intent)…")
            else:
                print(f"Semantic merge: {len(merge_input)} → группировка по смыслу…")
            t0 = time.time()
            output_entries = run_semantic_merge(
                merge_input,
                temperature=args.temperature,
                batch_size=args.semantic_batch_size,
                force=args.force_semantic,
                workers=args.semantic_workers,
            )
            print(f"  после semantic: {len(output_entries)} записей ({time.time() - t0:.0f} с)")

    write_csv(output_entries, CSV_PATH)
    frequent = [e for e in output_entries if e.frequency >= args.min_frequency]
    write_csv(frequent, CSV_FREQUENT_PATH)
    write_csv(output_entries, CSV_REVIEW_PATH)

    print(f"CSV (полный): {CSV_PATH} ({len(output_entries)} строк)")
    print(f"CSV (частые, freq≥{args.min_frequency}): {CSV_FREQUENT_PATH} ({len(frequent)} строк)")
    print(f"CSV (review): {CSV_REVIEW_PATH} ({len(output_entries)} строк)")

    write_stats(
        threads_total=two_way_n,
        threads_processed=threads_processed,
        raw_pairs=len(raw_pairs) or manifest.get("raw_pairs_count", 0),
        canonical=output_entries,
        allowed_clients=allowed_n,
        messages=messages_n,
        semantic_count=len(output_entries),
    )
    print(f"Статистика: {STATS_PATH}")
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

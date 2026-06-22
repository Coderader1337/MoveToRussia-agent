"""
Справочник вопросов клиентов (v4) → CSV для Excel.

Вместо полной LLM-базы знаний v4: извлекаем вопросы клиентов, группируем похожие,
считаем frequency и подбираем лучший реальный ответ менеджера из переписки.
Редкие (1×), но развёрнутые ответы не отбрасываются — ранжируются по качеству.

Вход:  mailbox_export_clean/threads/*.txt  (очищенные переписки)
        + knowledge_base/clients_stats/Clients_stats_v4.csv
        Fallback: mailbox_export/all_messages.jsonl (--jsonl)
Выход: knowledge_base/v4/client_faq.csv                 — все сгруппированные вопросы
       knowledge_base/v4/client_faq_frequent.csv        — frequency ≥ 2 (основной для заказчика)
       knowledge_base/v4/client_faq_quality_once.csv    — редкие 1× с развёрнутым ответом
       knowledge_base/v4/client_faq_polished.csv         — после --llm (частые, отполированные)
       knowledge_base/v4/faq_build_stats.json
       knowledge_base/v4/_faq_intermediate/      — кэш шагов

Шаги:
  1) extract + cluster + csv  — быстро, без API
  2) --llm                    — полировка формулировок и ответов через DeepSeek

Примеры:
  python build_faq_catalog.py --force
  python build_faq_catalog.py --llm --force
  python build_faq_catalog.py --min-frequency 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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

from build_kb_versions import filter_messages, read_allowed_clients, write_manifest  # noqa: E402
from build_knowledge_base import (  # noqa: E402
    DEEPSEEK_TEMPERATURE,
    STOPWORDS,
    call_deepseek,
    group_threads,
    load_messages,
)

KB_V4 = ROOT / "knowledge_base" / "v4"
INTERMEDIATE = KB_V4 / "_faq_intermediate"
QA_PAIRS_PATH = INTERMEDIATE / "qa_pairs.jsonl"
CLUSTERS_PATH = INTERMEDIATE / "clusters.json"
STATS_PATH = KB_V4 / "faq_build_stats.json"
CSV_PATH = KB_V4 / "client_faq.csv"
CSV_FREQUENT_PATH = KB_V4 / "client_faq_frequent.csv"
CSV_RARE_PATH = KB_V4 / "client_faq_quality_once.csv"
CSV_LLM_PATH = KB_V4 / "client_faq_polished.csv"

QUOTE_LINE = re.compile(r"^\s*>")
REPLY_CHAIN = re.compile(
    r"(?:^|\n)(?:-{3,}|_{3,}|={3,})\s*\n|"
    r"(?:^|\n)(?:Кому:|Тема:|Копия:|От:|"
    r"To:|Cc:|Bcc:|Subject:|From:|Sent:|Date:)\s|"
    r"(?:^|\n)On .+ wrote:\s*$|"
    r"(?:^|\n).+\@.+\.(?:com|ru|org|net).+ wrote:\s*$",
    re.I | re.M,
)
SIGNATURE_MARK = re.compile(
    r"\n(?:--+\s*\n|kind regards|warm regards|best regards|"
    r"client relationship manager|www\.movetorussia\.com)",
    re.I | re.M,
)
QUESTION_START = re.compile(
    r"^(?:what|how|when|where|why|who|can|could|would|do|does|did|is|are|was|were|"
    r"will|have|has|should|may|might|if|am i|are we|is it|is there|any)\b",
    re.I,
)
NOISE_QUESTION = re.compile(
    r"^(?:could you please correct|is that correct\??|is the accurate\??|"
    r"correct\??|dear |thank you|thanks |hi |hello |have a (?:wonderful|great|nice) |"
    r"wishing you|any updates will|if you don'?t want to receive|"
    r"\.{3,}|;\s*\d{2}\.\d{2}\.\d{4})",
    re.I,
)
MANAGER_TEMPLATE_Q = re.compile(
    r"(?:what is your (?:potential )?desired timeline|"
    r"are you in the exploration|have you already decided to relocate|"
    r"have you previously visited russia|"
    r"do you only need assistance with the relocation|"
    r"how many family members plan to move|"
    r"could you kindly provide a scan of your passport|"
    r"could you share a bit more (?:about )?your situation|"
    r"looking forward to your reply|please let us know your availability)",
    re.I,
)
EMAIL_HEADER_FRAGMENT = re.compile(
    r"(?:@\w+\.(?:com|ru|gmail|yandex)|\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}|"
    r"thank you kindly for your inquiry)",
    re.I,
)
CALL_ONLY_ANSWER = re.compile(
    r"^(?:please let us know your availability|would you be available for a (?:brief )?call|"
    r"as a next step, may i suggest we schedule a call|"
    r"if this service interests you, please feel free to contact us)",
    re.I,
)

LLM_SYSTEM = (
    "Ты методолог MoveToRussia.com. Получаешь группы реальных вопросов клиентов "
    "и черновики ответов менеджеров из переписки. Для каждой группы: "
    "1) сформулируй один канонический вопрос на английском (как его задал бы клиент); "
    "2) собери лучший ответ, опираясь ТОЛЬКО на предоставленные тексты ответов — "
    "ничего не выдумывай, суммы/сроки/ссылки сохраняй; "
    "3) если ответов несколько — объедини без потери фактов. "
    "Ответ на английском, как в переписке с клиентами."
)

LLM_BATCH_TEMPLATE = """Обработай каждую группу. Верни JSON-массив объектов:
{{"id": <номер группы>, "question": "...", "answer": "..."}}

Группы:
{batch}
"""


@dataclass
class QAPair:
    question: str
    answer: str
    client: str
    signature: frozenset[str]
    answer_score: float = 0.0


@dataclass
class Cluster:
    cluster_id: int
    frequency: int
    representative_question: str
    signature: frozenset[str]
    pairs: list[QAPair] = field(default_factory=list)
    best_answer: str = ""
    best_answer_score: float = 0.0
    question_variants: list[str] = field(default_factory=list)


def _trim_reply_chain(text: str) -> str:
    """Лёгкая обрезка: данные уже из mailbox_export_clean, только подпись."""
    if not text:
        return ""
    body = SIGNATURE_MARK.split(text, maxsplit=1)[0]
    return body.strip()


def _clean_body(text: str, *, multiline: bool = False) -> str:
    body = _trim_reply_chain(text or "")
    if not body:
        return ""
    if multiline:
        return body
    return re.sub(r"\s+", " ", body).strip()


def _sentences(text: str) -> list[str]:
    text = _clean_body(text, multiline=True)
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", flat)
    return [p.strip() for p in parts if p.strip()]


def _question_signature(q: str) -> frozenset[str]:
    words = re.findall(r"[a-zA-Z']{3,}", q.lower())
    sig = [w for w in words if w not in STOPWORDS]
    return frozenset(sig[:10])


def _content_word_count(s: str) -> int:
    words = re.findall(r"[a-zA-Z']{3,}", s.lower())
    return len([w for w in words if w not in STOPWORDS])


def _looks_like_question(s: str) -> bool:
    s = s.strip()
    if len(s) < 15 or len(s) > 380:
        return False
    if NOISE_QUESTION.search(s):
        return False
    if MANAGER_TEMPLATE_Q.search(s):
        return False
    if EMAIL_HEADER_FRAGMENT.search(s) and len(s) > 120:
        return False
    if _content_word_count(s) < 4:
        return False
    if not s.endswith("?"):
        return False
    return True


def _extract_questions(text: str) -> list[str]:
    found: list[str] = []
    for s in _sentences(text):
        if _looks_like_question(s):
            found.append(s)
    return found


def _score_answer(text: str) -> float:
    t = _clean_body(text)
    if not t:
        return 0.0
    if REPLY_CHAIN.search(text):
        t = _clean_body(text.split("\n----")[0] if "\n----" in text else text[:2500])
    t = t[:2500]
    score = min(len(t), 2500) / 120.0
    if re.search(r"\d", t):
        score += 1.5
    if re.search(r"(?:^|\n)\s*[-•*]\s", t):
        score += 2.5
    if re.search(r"https?://", t):
        score += 1.0
    if len(t.split()) >= 80:
        score += 2.0
    preview = t[:220]
    if CALL_ONLY_ANSWER.search(preview):
        score -= 4.0
    if len(t) < 80:
        score -= 1.0
    return score


def _pick_reply_text(thread: list[dict[str, Any]], after_idx: int) -> str:
    for m in thread[after_idx + 1 :]:
        if m.get("direction") != "outgoing":
            continue
        body = _clean_body(m.get("text", "") or "")
        if len(body) >= 80:
            return body[:2500]
    return ""


def extract_qa_pairs(
    threads: dict[str, list[dict[str, Any]]],
) -> list[QAPair]:
    pairs: list[QAPair] = []
    for client, thread in threads.items():
        for idx, msg in enumerate(thread):
            if msg.get("direction") != "incoming":
                continue
            for q in _extract_questions(msg.get("text", "") or ""):
                answer = _pick_reply_text(thread, idx)
                if not answer:
                    continue
                sig = _question_signature(q)
                if len(sig) < 2:
                    continue
                pair = QAPair(
                    question=q,
                    answer=answer,
                    client=client,
                    signature=sig,
                    answer_score=_score_answer(answer),
                )
                pairs.append(pair)
    return pairs


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _should_merge(a: frozenset[str], b: frozenset[str], threshold: float) -> bool:
    j = _jaccard(a, b)
    if j >= threshold:
        return True
    inter = len(a & b)
    if inter >= 3 and inter / min(len(a), len(b)) >= 0.55:
        return True
    return False


def cluster_pairs(pairs: list[QAPair], *, threshold: float) -> list[Cluster]:
    n = len(pairs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge(pairs[i].signature, pairs[j].signature, threshold):
                union(i, j)

    groups: dict[int, list[QAPair]] = defaultdict(list)
    for i, p in enumerate(pairs):
        groups[find(i)].append(p)

    clusters: list[Cluster] = []
    for cid, (root, group) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1):
        q_counter = Counter(p.question for p in group)
        rep_q = min(q_counter.keys(), key=lambda q: (len(q), q))
        sig = frozenset()
        for p in group:
            sig = sig | p.signature
        best = max(group, key=lambda p: (p.answer_score, len(p.answer)))
        variants = [q for q, _ in q_counter.most_common(5)]
        clusters.append(
            Cluster(
                cluster_id=cid,
                frequency=len(group),
                representative_question=rep_q,
                signature=sig,
                pairs=group,
                best_answer=best.answer,
                best_answer_score=best.answer_score,
                question_variants=variants,
            )
        )

    clusters.sort(key=lambda c: (-c.frequency, -c.best_answer_score, c.representative_question))
    for i, c in enumerate(clusters, start=1):
        c.cluster_id = i
    return clusters


def filter_clusters(clusters: list[Cluster], *, min_answer_score: float) -> list[Cluster]:
    return [
        c for c in clusters
        if c.frequency >= 2 or c.best_answer_score >= min_answer_score
    ]


def write_csv(clusters: list[Cluster], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(["number", "question", "frequency", "answer"])
        for i, c in enumerate(clusters, start=1):
            w.writerow([
                i,
                c.representative_question,
                c.frequency,
                _clean_body(c.best_answer),
            ])


def _is_quality_singleton(c: Cluster, *, min_score: float, min_chars: int) -> bool:
    """Редкий вопрос (1×) с развёрнутым ответом — не теряем «жемчужины»."""
    ans = _clean_body(c.best_answer)
    if len(ans) < min_chars:
        return False
    if CALL_ONLY_ANSWER.search(ans[:320]):
        return False
    return c.best_answer_score >= min_score


def split_outputs(clusters: list[Cluster], *, min_frequency: int, rare_min_score: float,
                  rare_min_chars: int) -> tuple[list[Cluster], list[Cluster], list[Cluster]]:
    frequent = [c for c in clusters if c.frequency >= min_frequency]
    rare = [
        c for c in clusters
        if c.frequency == 1 and _is_quality_singleton(
            c, min_score=rare_min_score, min_chars=rare_min_chars,
        )
    ]
    review = sorted(frequent + rare, key=lambda c: (-c.frequency, -c.best_answer_score))
    return frequent, rare, review


def save_intermediate(pairs: list[QAPair], clusters: list[Cluster]) -> None:
    INTERMEDIATE.mkdir(parents=True, exist_ok=True)
    with QA_PAIRS_PATH.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(
                json.dumps(
                    {
                        "question": p.question,
                        "answer": p.answer,
                        "client": p.client,
                        "signature": sorted(p.signature),
                        "answer_score": p.answer_score,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    payload = [
        {
            "cluster_id": c.cluster_id,
            "frequency": c.frequency,
            "representative_question": c.representative_question,
            "question_variants": c.question_variants,
            "best_answer": c.best_answer,
            "best_answer_score": c.best_answer_score,
        }
        for c in clusters
    ]
    CLUSTERS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_clusters_from_cache() -> list[Cluster]:
    data = json.loads(CLUSTERS_PATH.read_text(encoding="utf-8"))
    clusters: list[Cluster] = []
    for row in data:
        clusters.append(
            Cluster(
                cluster_id=row["cluster_id"],
                frequency=row["frequency"],
                representative_question=row["representative_question"],
                signature=frozenset(),
                best_answer=row["best_answer"],
                best_answer_score=row["best_answer_score"],
                question_variants=row.get("question_variants", []),
            )
        )
    return clusters


def _llm_batch(clusters: list[Cluster], *, temperature: float) -> list[dict[str, Any]]:
    lines: list[str] = []
    for c in clusters:
        variants = " | ".join(c.question_variants[:3])
        answers = sorted({p.answer[:1200] for p in c.pairs}, key=len, reverse=True)[:3]
        if not answers:
            answers = [c.best_answer[:1200]]
        ans_block = "\n---\n".join(answers)
        lines.append(
            f"ID {c.cluster_id} (frequency={c.frequency})\n"
            f"Варианты вопроса: {variants}\n"
            f"Ответы менеджеров:\n{ans_block}\n"
        )
    user = LLM_BATCH_TEMPLATE.format(batch="\n\n".join(lines))
    raw = call_deepseek(LLM_SYSTEM, user, temperature=temperature)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def polish_with_llm(
    clusters: list[Cluster],
    *,
    temperature: float,
    batch_size: int = 8,
) -> list[Cluster]:
    polished: list[Cluster] = []
    total_batches = (len(clusters) + batch_size - 1) // batch_size
    for bi, start in enumerate(range(0, len(clusters), batch_size), start=1):
        batch = clusters[start : start + batch_size]
        print(f"  LLM batch {bi}/{total_batches} ({len(batch)} групп)…")
        t0 = time.time()
        try:
            results = _llm_batch(batch, temperature=temperature)
        except Exception as exc:
            print(f"    ошибка LLM: {exc}", file=sys.stderr)
            polished.extend(batch)
            continue
        by_id = {int(r["id"]): r for r in results if "id" in r}
        for c in batch:
            row = by_id.get(c.cluster_id)
            if row:
                c.representative_question = (row.get("question") or c.representative_question).strip()
                c.best_answer = (row.get("answer") or c.best_answer).strip()
            polished.append(c)
        print(f"    готово за {time.time() - t0:.0f} c")
        time.sleep(1)
    return polished


def _copy_cluster(c: Cluster) -> Cluster:
    return Cluster(
        cluster_id=c.cluster_id,
        frequency=c.frequency,
        representative_question=c.representative_question,
        signature=c.signature,
        pairs=list(c.pairs),
        best_answer=c.best_answer,
        best_answer_score=c.best_answer_score,
        question_variants=list(c.question_variants),
    )


def write_stats(
    *,
    qa_pairs_count: int,
    clusters_all: list[Cluster],
    clusters_out: list[Cluster],
    clusters_frequent: int,
    clusters_rare: int,
    allowed_clients: int,
    messages: int,
    llm_polished: bool,
    message_source: str,
) -> None:
    freq_hist = Counter(c.frequency for c in clusters_out)
    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "message_source": message_source,
        "clients_in_scope": allowed_clients,
        "messages_in_scope": messages,
        "qa_pairs_extracted": qa_pairs_count,
        "clusters_before_filter": len(clusters_all),
        "clusters_in_csv_full": len(clusters_out),
        "clusters_frequent": clusters_frequent,
        "clusters_rare_quality": clusters_rare,
        "singleton_clusters": sum(1 for c in clusters_out if c.frequency == 1),
        "frequency_distribution": dict(sorted(freq_hist.items())),
        "llm_polished": llm_polished,
        "outputs": {
            "csv_full": str(CSV_PATH.relative_to(ROOT)),
            "csv_frequent": str(CSV_FREQUENT_PATH.relative_to(ROOT)),
            "csv_rare_quality": str(CSV_RARE_PATH.relative_to(ROOT)),
            "csv_polished": str(CSV_LLM_PATH.relative_to(ROOT)),
            "intermediate": str(INTERMEDIATE.relative_to(ROOT)),
        },
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm", action="store_true",
                   help="Полировка question/answer через DeepSeek → client_faq_polished.csv")
    p.add_argument("--jsonl", action="store_true",
                   help="Брать письма из mailbox_export/all_messages.jsonl вместо clean")
    p.add_argument("--force", action="store_true", help="Пересобрать extract/cluster без кэша")
    p.add_argument("--cluster-threshold", type=float, default=0.34,
                   help="Порог похожести вопросов (Jaccard), по умолчанию 0.34")
    p.add_argument("--min-answer-score", type=float, default=4.0,
                   help="Мин. качество ответа для включения в полный CSV (1×)")
    p.add_argument("--min-frequency", type=int, default=2,
                   help="Порог frequency для client_faq_frequent.csv")
    p.add_argument("--rare-min-score", type=float, default=18.0,
                   help="Мин. score ответа для client_faq_quality_once.csv")
    p.add_argument("--rare-min-chars", type=int, default=900,
                   help="Мин. длина ответа для client_faq_quality_once.csv")
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    p.add_argument("--llm-batch-size", type=int, default=8)
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("=== FAQ-справочник v4 ===")
    print("Загрузка писем…")
    all_messages = load_messages(prefer_clean=not args.jsonl)
    allowed, flagged_n, _ = read_allowed_clients(4)
    messages = filter_messages(all_messages, allowed)
    threads = group_threads(messages)
    print(f"  адресов: {len(allowed)} | писем: {len(messages)} | диалогов: {len(threads)}")

    KB_V4.mkdir(parents=True, exist_ok=True)
    write_manifest(KB_V4, messages)

    clusters_all: list[Cluster]
    if QA_PAIRS_PATH.is_file() and CLUSTERS_PATH.is_file() and not args.force:
        print("Extract/cluster: из кэша (_faq_intermediate/)")
        clusters = load_clusters_from_cache()
        qa_pairs_count = sum(1 for _ in QA_PAIRS_PATH.open(encoding="utf-8"))
        clusters_all = clusters
    else:
        print("Extract: вопросы клиентов + ответы менеджеров…")
        pairs = extract_qa_pairs(threads)
        qa_pairs_count = len(pairs)
        print(f"  пар Q→A: {qa_pairs_count}")
        print(f"Cluster: порог {args.cluster_threshold}…")
        clusters_all = cluster_pairs(pairs, threshold=args.cluster_threshold)
        clusters = filter_clusters(clusters_all, min_answer_score=args.min_answer_score)
        print(f"  кластеров всего: {len(clusters_all)} → в CSV: {len(clusters)}")
        save_intermediate(pairs, clusters)

    write_csv(clusters, CSV_PATH)
    print(f"CSV (полный): {CSV_PATH} ({len(clusters)} строк)")

    frequent, rare, review = split_outputs(
        clusters,
        min_frequency=args.min_frequency,
        rare_min_score=args.rare_min_score,
        rare_min_chars=args.rare_min_chars,
    )
    write_csv(frequent, CSV_FREQUENT_PATH)
    write_csv(rare, CSV_RARE_PATH)
    print(f"CSV (частые, freq≥{args.min_frequency}): {CSV_FREQUENT_PATH} ({len(frequent)} строк)")
    print(f"CSV (редкие качественные 1×): {CSV_RARE_PATH} ({len(rare)} строк)")

    llm_polished = False
    llm_input = review
    if args.llm:
        print(f"LLM: полировка frequent ({len(frequent)} групп)…")
        llm_input = polish_with_llm(
            [_copy_cluster(c) for c in frequent],
            temperature=args.temperature,
            batch_size=args.llm_batch_size,
        )
        write_csv(llm_input, CSV_LLM_PATH)
        print(f"CSV (LLM): {CSV_LLM_PATH} ({len(llm_input)} строк)")
        llm_polished = True

    write_stats(
        qa_pairs_count=qa_pairs_count,
        clusters_all=clusters_all,
        clusters_out=clusters,
        clusters_frequent=len(frequent),
        clusters_rare=len(rare),
        allowed_clients=len(allowed),
        messages=len(messages),
        llm_polished=llm_polished,
        message_source="jsonl" if args.jsonl else "mailbox_export_clean",
    )

    print(f"Статистика: {STATS_PATH}")
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

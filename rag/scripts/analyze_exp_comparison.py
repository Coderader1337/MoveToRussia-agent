"""Analyze exp_comparison.csv: compare prod (answer) vs exp (exp_answer, with Telegram data)."""
from __future__ import annotations

import csv
import difflib
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "exp_comparison.csv"


def load_rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    rows = load_rows()
    n = len(rows)
    print(f"n_rows={n}")

    lens_a = [len(r["answer"]) for r in rows]
    lens_e = [len(r["exp_answer"]) for r in rows]
    print(f"avg_len answer={statistics.mean(lens_a):.1f} exp={statistics.mean(lens_e):.1f}")
    print(f"median_len answer={statistics.median(lens_a):.1f} exp={statistics.median(lens_e):.1f}")

    empty_a = sum(1 for r in rows if not r["answer"].strip())
    empty_e = sum(1 for r in rows if not r["exp_answer"].strip())
    identical = sum(1 for r in rows if r["answer"].strip() == r["exp_answer"].strip())
    print(f"empty_answer={empty_a} empty_exp={empty_e} identical={identical}")

    # similarity ratio between answer and exp_answer
    ratios = []
    for r in rows:
        a, e = r["answer"].strip(), r["exp_answer"].strip()
        if not a and not e:
            ratios.append(1.0)
            continue
        ratios.append(difflib.SequenceMatcher(None, a, e).ratio())
    print(f"avg_similarity={statistics.mean(ratios):.3f} median={statistics.median(ratios):.3f}")

    # bucket by similarity
    buckets = Counter()
    for ratio in ratios:
        if ratio > 0.9:
            buckets["near_identical(>0.9)"] += 1
        elif ratio > 0.7:
            buckets["similar(0.7-0.9)"] += 1
        elif ratio > 0.4:
            buckets["moderate_diff(0.4-0.7)"] += 1
        else:
            buckets["very_different(<0.4)"] += 1
    print("similarity_buckets:", dict(buckets))

    # signals of Telegram-influenced content: informal markers, price/date mentions
    def signals(text: str) -> Counter:
        c = Counter()
        c["has_number"] = int(bool(re.search(r"\d", text)))
        c["len_words"] = len(text.split())
        return c

    # errors / fallback answers
    fallback_patterns = [
        "не найдено", "нет информации", "not found", "no relevant", "don't have",
        "i don't know", "not sure", "cannot find", "no context",
    ]
    fb_a = sum(1 for r in rows if any(p in r["answer"].lower() for p in fallback_patterns))
    fb_e = sum(1 for r in rows if any(p in r["exp_answer"].lower() for p in fallback_patterns))
    print(f"fallback_like answer={fb_a} exp={fb_e}")

    # word count diff
    wc_a = [len(r["answer"].split()) for r in rows]
    wc_e = [len(r["exp_answer"].split()) for r in rows]
    print(f"avg_words answer={statistics.mean(wc_a):.1f} exp={statistics.mean(wc_e):.1f}")

    # Most different rows (lowest similarity) - save for manual review
    scored = sorted(zip(ratios, rows), key=lambda x: x[0])
    out_dir = ROOT / "data"
    with (out_dir / "exp_most_different.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["similarity", "question", "answer", "exp_answer"])
        for ratio, r in scored[:60]:
            w.writerow([f"{ratio:.3f}", r["question"], r["answer"], r["exp_answer"]])
    print("wrote data/exp_most_different.csv (60 most-different rows)")

    # near identical count as % 
    print(f"pct_identical_or_near = {(identical + buckets['near_identical(>0.9)']) / n * 100:.1f}%")


if __name__ == "__main__":
    main()

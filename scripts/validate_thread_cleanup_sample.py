"""Быстрая глубокая проверка на выборке и самых «раздутых» переписках."""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from clean_thread_quotes import parse_thread_file, render_thread, strip_quotes

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "mailbox_export" / "threads"
CLEAN = ROOT / "mailbox_export_clean" / "threads"
STATS = ROOT / "mailbox_export_clean" / "quote_cleanup_stats.json"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def check_file(orig_path: Path, clean_path: Path) -> list[str]:
    issues: list[str] = []
    orig_text = orig_path.read_text(encoding="utf-8")
    clean_text = clean_path.read_text(encoding="utf-8")

    client, count, blocks = parse_thread_file(orig_text)
    expected_render = render_thread(client, count, blocks)
    if expected_render != clean_text:
        issues.append("roundtrip_render_mismatch")

    clean_blocks = parse_thread_file(clean_text)[2]
    bodies_clean = [norm(strip_quotes(b.body)[0]) for b in blocks]

    for idx, (ob, cb) in enumerate(zip(blocks, clean_blocks), start=1):
        expected, removed = strip_quotes(ob.body)
        actual = cb.body.strip()
        if actual == "(пустое тело)":
            actual = ""

        if expected != actual:
            issues.append(f"msg{idx}:body_mismatch")

        if removed > 100:
            tail = norm(ob.body[len(expected) :])
            sample = tail[60:220] if len(tail) > 220 else tail[20:140]
            if len(sample) >= 30:
                if not any(sample in b for j, b in enumerate(bodies_clean) if j != idx - 1):
                    issues.append(f"msg{idx}:removed_sample_not_in_thread:{sample[:50]!r}")

    return issues


def main() -> int:
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    top = [r["file"] for r in stats["top_bloated"][:25]]
    all_files = sorted(p.name for p in SOURCE.glob("*.txt"))
    random.seed(42)
    sample = random.sample(all_files, min(100, len(all_files)))
    must = ["1james.1_tutanota.com.txt", "tristanchase_me.com.txt"]
    targets = sorted(set(top + sample + must))

    bad: list[dict] = []
    for name in targets:
        issues = check_file(SOURCE / name, CLEAN / name)
        if issues:
            bad.append({"file": name, "issues": issues})

    print("=== Targeted deep validation ===")
    print(f"Checked: {len(targets)} files (top-25 + random-100 + must-have)")
    print(f"Files with issues: {len(bad)}")
    if bad:
        for row in bad[:20]:
            print(f"  {row['file']}: {row['issues'][:3]}")
        return 1
    print("PASS: roundtrip + duplicate checks OK on sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

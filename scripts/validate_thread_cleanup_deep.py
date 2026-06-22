"""Глубокая проверка: удалённый текст — дубликат других писем в той же переписке."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from clean_thread_quotes import parse_thread_file, strip_quotes

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "mailbox_export" / "threads"
CLEAN = ROOT / "mailbox_export_clean" / "threads"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def sample_from_tail(tail: str, min_len: int = 40) -> str | None:
    t = norm(tail)
    if len(t) < min_len:
        return None
    # Пропускаем типичные заголовки цитат
    for skip in (80, 60, 40, 20):
        chunk = t[skip : skip + 120]
        if len(chunk) >= min_len and not chunk.startswith("to:"):
            return chunk
    return t[20:140] if len(t) > 140 else None


def validate_file(orig_path: Path, clean_path: Path) -> list[dict]:
    issues: list[dict] = []
    orig_blocks = parse_thread_file(orig_path.read_text(encoding="utf-8"))[2]
    clean_blocks = parse_thread_file(clean_path.read_text(encoding="utf-8"))[2]

    cleaned_pool: list[str] = []
    for idx, (ob, cb) in enumerate(zip(orig_blocks, clean_blocks), start=1):
        expected, removed = strip_quotes(ob.body)
        actual = cb.body.strip()
        if actual == "(пустое тело)":
            actual = ""

        if expected != actual:
            issues.append({"msg": idx, "code": "body_mismatch"})

        exp_n = norm(expected)
        if exp_n and exp_n not in norm(ob.body):
            issues.append({"msg": idx, "code": "invented_content"})

        if removed > 100:
            tail = ob.body[len(expected) :]
            sample = sample_from_tail(tail)
            if sample:
                in_thread = any(sample in c for c in cleaned_pool) or any(
                    sample in norm(strip_quotes(b.body)[0])
                    for j, b in enumerate(orig_blocks)
                    if j != idx - 1
                )
                if not in_thread:
                    issues.append(
                        {
                            "msg": idx,
                            "code": "removed_not_found_elsewhere",
                            "sample": sample[:80],
                        }
                    )

        if exp_n:
            cleaned_pool.append(exp_n)

    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--clean", type=Path, default=CLEAN)
    args = parser.parse_args()

    all_issues: list[dict] = []
    files_with_issues = 0
    trimmed_checked = 0

    for orig_path in sorted(args.source.glob("*.txt")):
        clean_path = args.clean / orig_path.name
        if not clean_path.exists():
            continue
        issues = validate_file(orig_path, clean_path)
        if issues:
            files_with_issues += 1
            for iss in issues:
                iss["file"] = orig_path.name
                all_issues.append(iss)
        _, _, blocks = parse_thread_file(orig_path.read_text(encoding="utf-8"))
        trimmed_checked += sum(1 for b in blocks if strip_quotes(b.body)[1] > 100)

    report = {
        "passed": len(all_issues) == 0,
        "files_checked": len(list(args.source.glob("*.txt"))),
        "files_with_issues": files_with_issues,
        "issues_total": len(all_issues),
        "trimmed_messages": trimmed_checked,
        "samples": all_issues[:40],
    }
    out = ROOT / "mailbox_export_clean" / "deep_validation_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== Deep duplicate validation ===")
    print(f"Files: {report['files_checked']}, trimmed msgs: {trimmed_checked}")
    print(f"Issues: {len(all_issues)} in {files_with_issues} files")
    if all_issues:
        for iss in all_issues[:15]:
            print(f"  {iss['file']} #{iss['msg']} [{iss['code']}]: {iss.get('sample', '')}")
        return 1
    print("PASS")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Валидация очищенных переписок mailbox_export_clean/threads против исходников.

Проверяет структуру, заголовки, побайтовое соответствие strip_quotes() и
отсутствие потери уникального содержимого.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from clean_thread_quotes import parse_thread_file, strip_quotes

ROOT = Path(__file__).resolve().parent.parent
SOURCE_THREADS = ROOT / "mailbox_export" / "threads"
CLEAN_THREADS = ROOT / "mailbox_export_clean" / "threads"
REPORT_PATH = ROOT / "mailbox_export_clean" / "validation_report.json"


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _header_key(block) -> tuple[str, ...]:
    # Заголовки без разделителя тела
    return tuple(block.header_lines)


def _extract_meta(header_lines: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in header_lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip()
    return meta


@dataclass
class Issue:
    file: str
    message_index: int
    severity: str  # error | warn
    code: str
    detail: str


@dataclass
class ValidationStats:
    files_checked: int = 0
    messages_checked: int = 0
    messages_stripped: int = 0
    bytes_removed: int = 0
    issues: list[Issue] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warn"]


def validate_pair(orig_path: Path, clean_path: Path, stats: ValidationStats) -> None:
    fname = orig_path.name
    stats.files_checked += 1

    orig_text = orig_path.read_text(encoding="utf-8")
    clean_text = clean_path.read_text(encoding="utf-8")

    orig_client, orig_count, orig_blocks = parse_thread_file(orig_text)
    clean_client, clean_count, clean_blocks = parse_thread_file(clean_text)

    if orig_client != clean_client:
        stats.add(Issue(fname, 0, "error", "client_mismatch", f"{orig_client!r} != {clean_client!r}"))

    if orig_count != clean_count:
        stats.add(
            Issue(fname, 0, "error", "count_mismatch", f"ПИСЕМ: {orig_count} != {clean_count}")
        )

    if len(orig_blocks) != len(clean_blocks):
        stats.add(
            Issue(
                fname,
                0,
                "error",
                "message_count_mismatch",
                f"{len(orig_blocks)} != {len(clean_blocks)}",
            )
        )
        return

    # Собираем все очищенные тела для проверки «удалённое = дубликат»
    all_clean_bodies_norm: list[str] = []

    for idx, (ob, cb) in enumerate(zip(orig_blocks, clean_blocks), start=1):
        stats.messages_checked += 1

        if _header_key(ob) != _header_key(cb):
            stats.add(
                Issue(
                    fname,
                    idx,
                    "error",
                    "header_changed",
                    "Заголовок письма изменился",
                )
            )

        expected, removed = strip_quotes(ob.body)
        actual = cb.body.strip()
        if actual == "(пустое тело)":
            actual = ""

        if expected != actual:
            stats.add(
                Issue(
                    fname,
                    idx,
                    "error",
                    "body_mismatch",
                    f"Тело не совпадает с strip_quotes(): "
                    f"expected {len(expected)} chars, got {len(actual)} chars",
                )
            )

        if removed > 0:
            stats.messages_stripped += 1
            stats.bytes_removed += removed

        # Очищенное тело должно быть префиксом исходного (после rstrip)
        orig_stripped = ob.body.rstrip()
        if expected and expected != orig_stripped:
            if not orig_stripped.startswith(expected):
                stats.add(
                    Issue(
                        fname,
                        idx,
                        "error",
                        "not_a_prefix",
                        "Очищенный текст не является префиксом исходного",
                    )
                )

        # Пустое тело при непустом оригинале
        orig_meaningful = _norm_ws(ob.body)
        clean_meaningful = _norm_ws(expected)
        if len(orig_meaningful) > 80 and len(clean_meaningful) < 20:
            stats.add(
                Issue(
                    fname,
                    idx,
                    "error",
                    "over_stripped",
                    f"Оригинал {len(orig_meaningful)} симв., осталось {len(clean_meaningful)}",
                )
            )

        # Подозрительно большая обрезка без маркера цитаты в хвосте
        if len(orig_stripped) > 200 and len(expected) < len(orig_stripped) * 0.15:
            removed_tail = orig_stripped[len(expected) :].strip()
            if len(removed_tail) > 100:
                # Хвост не должен содержать уникальный текст, которого нет в других письмах
                tail_sample = _norm_ws(removed_tail)[:120]
                stats.add(
                    Issue(
                        fname,
                        idx,
                        "warn",
                        "heavy_trim",
                        f"Удалено {100 * (1 - len(expected)/len(orig_stripped)):.0f}% текста; "
                        f"хвост начинается: {tail_sample[:80]!r}...",
                    )
                )

        if clean_meaningful:
            all_clean_bodies_norm.append(clean_meaningful)

    # Хронология дат не должна меняться (заголовки те же — достаточно проверить порядок дат)
    dates = [_extract_meta(b.header_lines).get("Дата", "") for b in clean_blocks]
    if dates != [_extract_meta(b.header_lines).get("Дата", "") for b in orig_blocks]:
        stats.add(Issue(fname, 0, "error", "order_changed", "Порядок писем изменился"))


def validate_all(source: Path, clean: Path) -> ValidationStats:
    stats = ValidationStats()

    orig_files = sorted(source.glob("*.txt"))
    clean_files = {p.name for p in clean.glob("*.txt")}
    orig_names = {p.name for p in orig_files}

    missing = orig_names - clean_files
    extra = clean_files - orig_names
    for name in sorted(missing):
        stats.add(Issue(name, 0, "error", "missing_clean_file", "Нет очищенного файла"))
    for name in sorted(extra):
        stats.add(Issue(name, 0, "error", "extra_clean_file", "Лишний файл в clean/"))

    for orig_path in orig_files:
        if orig_path.name not in clean_files:
            continue
        validate_pair(orig_path, clean / orig_path.name, stats)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_THREADS)
    parser.add_argument("--clean", type=Path, default=CLEAN_THREADS)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args()

    if not args.source.is_dir() or not args.clean.is_dir():
        print("Нужны обе папки: source и clean", file=sys.stderr)
        return 1

    stats = validate_all(args.source, args.clean)
    errors = stats.errors
    warnings = stats.warnings

    report = {
        "passed": len(errors) == 0,
        "files_checked": stats.files_checked,
        "messages_checked": stats.messages_checked,
        "messages_stripped": stats.messages_stripped,
        "bytes_removed": stats.bytes_removed,
        "errors": len(errors),
        "warnings": len(warnings),
        "error_samples": [
            {"file": e.file, "msg": e.message_index, "code": e.code, "detail": e.detail}
            for e in errors[:50]
        ],
        "warning_samples": [
            {"file": w.file, "msg": w.message_index, "code": w.code, "detail": w.detail}
            for w in warnings[:30]
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== Валидация очистки переписок ===")
    print(f"Файлов проверено:     {stats.files_checked}")
    print(f"Писем проверено:      {stats.messages_checked}")
    print(f"Писем с обрезкой:     {stats.messages_stripped}")
    print(f"Байт цитат удалено:   {stats.bytes_removed / 1024 / 1024:.1f} MB")
    print(f"Ошибок (critical):    {len(errors)}")
    print(f"Предупреждений:       {len(warnings)}")
    print()

    if errors:
        print("ОШИБКИ (первые 20):")
        for e in errors[:20]:
            print(f"  [{e.code}] {e.file} #{e.message_index}: {e.detail}")
        print()
        print(f"Полный отчёт: {args.report}")
        return 1

    print("РЕЗУЛЬТАТ: все проверки пройдены.")
    if warnings:
        print(f"\nПредупреждения ({len(warnings)}) — не ошибки, но стоит просмотреть:")
        for w in warnings[:10]:
            print(f"  [{w.code}] {w.file} #{w.message_index}: {w.detail}")
        if len(warnings) > 10:
            print(f"  ... и ещё {len(warnings) - 10}")

    print(f"\nОтчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

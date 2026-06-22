"""
Анализ и очистка вложенных цитат в экспортированных переписках mailbox_export/threads.

Исходники не трогаем — результат в mailbox_export_clean/threads/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SOURCE_THREADS = ROOT / "mailbox_export" / "threads"
OUTPUT_THREADS = ROOT / "mailbox_export_clean" / "threads"

MAIL_SEPARATOR = "=" * 78
INNER_RULE = "-" * 78

# Маркеры начала цитирования предыдущих писем (от более специфичных к общим).
QUOTE_START_PATTERNS: list[re.Pattern[str]] = [
    # Yandex (часто inline после подписи менеджера, не с начала строки)
    re.compile(
        r"-{10,}\s*(?:To:|Кому:)\s*.+;\s*(?:Subject:|Тема:)",
        re.IGNORECASE,
    ),
    re.compile(
        r"-{10,}\s*\d{2}\.\d{2}\.\d{4},\s+\d{1,2}:\d{2},",
        re.IGNORECASE,
    ),
    re.compile(
        r"-{10,}\s*\"[^\"]+\"\s*<[^>]+@[^>]+>\s*:",
        re.IGNORECASE,
    ),
    # Tutanota / Proton и похожие
    re.compile(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s+by\s+\S+@\S+\s*:",
        re.IGNORECASE,
    ),
    # Gmail / Apple Mail (строка целиком или inline)
    re.compile(r"^On .{10,80} wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"\nOn .{10,120} wrote:\s*\n", re.IGNORECASE),
    re.compile(r"^Le .{10,80} a écrit\s*:", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^Am .{10,80} schrieb .+:\s*$", re.MULTILINE | re.IGNORECASE),
    # Outlook
    re.compile(r"-{5,}\s*Original Message\s*-{5,}", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$", re.MULTILINE),
    # Forward / reply headers (только после разделителя цитаты)
    re.compile(
        r"-{10,}\s*(?:From:|От:)\s*.+@.+",
        re.IGNORECASE,
    ),
]

SIGNATURE_PATTERN = re.compile(r"\n\s*--\s*\n", re.MULTILINE)


def find_quote_start(text: str) -> int | None:
    """Позиция начала вложенной переписки в теле письма."""
    earliest: int | None = None

    for pat in QUOTE_START_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        pos = m.start()
        # Не режем «цитату» в первых 20 символах — это почти всегда ложное срабатывание.
        if pos < 20:
            continue
        if earliest is None or pos < earliest:
            earliest = pos

    if earliest is not None:
        return earliest

    # Подпись "--" + цитата (типично для исходящих менеджеров)
    sig = SIGNATURE_PATTERN.search(text)
    if sig and sig.start() >= 20:
        tail = text[sig.end() : sig.end() + 400]
        for pat in QUOTE_START_PATTERNS:
            if pat.search(tail):
                return sig.start()

    return None


def strip_quotes(text: str) -> tuple[str, int]:
    """Убрать вложенные цитаты. Возвращает (очищенный текст, байт снято)."""
    original = text.rstrip()
    pos = find_quote_start(original)
    if pos is None:
        cleaned = original.strip()
        removed = len(original.encode("utf-8")) - len(cleaned.encode("utf-8"))
        return cleaned, max(removed, 0)

    cleaned = original[:pos].rstrip()
    # Убрать хвостовую подпись "--" без содержимого
    cleaned = re.sub(r"\n\s*--\s*$", "", cleaned).rstrip()
    removed = len(original.encode("utf-8")) - len(cleaned.encode("utf-8"))
    return cleaned, removed


@dataclass
class MessageBlock:
    header_lines: list[str]
    body: str

    @property
    def cleaned_body(self) -> tuple[str, int]:
        return strip_quotes(self.body)


def parse_thread_file(text: str) -> tuple[str, str, list[MessageBlock]]:
    lines = text.splitlines()
    client = ""
    msg_count = ""
    if lines and lines[0].startswith("КЛИЕНТ:"):
        client = lines[0].split(":", 1)[1].strip()
    if len(lines) > 1 and lines[1].startswith("ПИСЕМ:"):
        msg_count = lines[1].split(":", 1)[1].strip()

    blocks: list[MessageBlock] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != MAIL_SEPARATOR:
            i += 1
            continue
        header: list[str] = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() != INNER_RULE:
            header.append(lines[i])
            i += 1
        if i >= len(lines):
            break
        header.append(lines[i])  # INNER_RULE
        i += 1
        body_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != MAIL_SEPARATOR:
            body_lines.append(lines[i])
            i += 1
        blocks.append(MessageBlock(header, "\n".join(body_lines)))
    return client, msg_count, blocks


def render_thread(client: str, msg_count: str, blocks: list[MessageBlock]) -> str:
    parts = [f"КЛИЕНТ: {client}", f"ПИСЕМ: {msg_count}", ""]
    for block in blocks:
        cleaned, _ = block.cleaned_body
        parts.extend(block.header_lines)
        parts.append(cleaned or "(пустое тело)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def analyze_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    orig_bytes = len(text.encode("utf-8"))
    client, msg_count, blocks = parse_thread_file(text)

    quoted_msgs = 0
    removed_total = 0
    for block in blocks:
        cleaned, removed = block.cleaned_body
        if removed > 50:
            quoted_msgs += 1
        removed_total += removed

    cleaned_text = render_thread(client, msg_count, blocks)
    clean_bytes = len(cleaned_text.encode("utf-8"))

    return {
        "file": path.name,
        "client": client,
        "messages": len(blocks),
        "orig_bytes": orig_bytes,
        "clean_bytes": clean_bytes,
        "removed_bytes": removed_total,
        "msgs_with_quotes": quoted_msgs,
        "reduction_pct": round(100 * (1 - clean_bytes / orig_bytes), 1) if orig_bytes else 0,
    }


def run_analysis(source: Path) -> dict:
    rows = [analyze_file(p) for p in sorted(source.glob("*.txt"))]
    total_orig = sum(r["orig_bytes"] for r in rows)
    total_clean = sum(r["clean_bytes"] for r in rows)
    total_msgs = sum(r["messages"] for r in rows)
    quoted_msgs = sum(r["msgs_with_quotes"] for r in rows)
    return {
        "files": len(rows),
        "total_messages": total_msgs,
        "messages_with_nested_quotes": quoted_msgs,
        "orig_size_mb": round(total_orig / 1024 / 1024, 2),
        "clean_size_mb": round(total_clean / 1024 / 1024, 2),
        "saved_mb": round((total_orig - total_clean) / 1024 / 1024, 2),
        "reduction_pct": round(100 * (1 - total_clean / total_orig), 1) if total_orig else 0,
        "top_bloated": sorted(rows, key=lambda r: r["removed_bytes"], reverse=True)[:20],
        "per_file": rows,
    }


def clean_all(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        client, msg_count, blocks = parse_thread_file(text)
        cleaned = render_thread(client, msg_count, blocks)
        (output / path.name).write_text(cleaned, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=SOURCE_THREADS,
        help="Папка с исходными threads/*.txt",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_THREADS,
        help="Папка для очищенных threads/*.txt",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Только анализ, без записи файлов",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"Нет папки: {args.source}", file=sys.stderr)
        return 1

    report = run_analysis(args.source)

    print("=== Анализ вложенных цитат ===")
    print(f"Файлов переписок: {report['files']}")
    print(f"Писем всего: {report['total_messages']}")
    print(
        f"Писем с вложенными цитатами (>50 симв.): "
        f"{report['messages_with_nested_quotes']} "
        f"({100 * report['messages_with_nested_quotes'] / max(report['total_messages'], 1):.1f}%)"
    )
    print(
        f"Размер threads: {report['orig_size_mb']} MB -> после очистки: "
        f"{report['clean_size_mb']} MB (-{report['saved_mb']} MB, -{report['reduction_pct']}%)"
    )
    print("\nТоп-15 переписок по объёму цитат:")
    for row in report["top_bloated"][:15]:
        print(
            f"  {row['file']}: {row['orig_bytes'] / 1024:.0f} KB -> "
            f"{row['clean_bytes'] / 1024:.0f} KB "
            f"({row['msgs_with_quotes']}/{row['messages']} писем с цитатами, "
            f"-{row['reduction_pct']}%)"
        )

    stats_path = ROOT / "mailbox_export_clean" / "quote_cleanup_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(
            {k: v for k, v in report.items() if k != "per_file"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not args.analyze_only:
        clean_all(args.source, args.output)
        print(f"\nОчищенные переписки: {args.output}")
        print(f"Статистика: {stats_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

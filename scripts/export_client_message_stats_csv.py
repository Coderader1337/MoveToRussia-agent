"""
Сгенерировать client_message_stats.csv из index.csv (без повторной выгрузки IMAP).

Формат: строка 1 — email клиентов, строка 2 — число писем в переписке.
Слева направо по убыванию количества писем.

  python export_client_message_stats_csv.py
  python export_client_message_stats_csv.py -i mailbox_export/index.csv -o out.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "mailbox_export" / "index.csv"
DEFAULT_OUTPUT = ROOT / "mailbox_export" / "client_message_stats.csv"


def build_from_index(index_path: Path, output_path: Path) -> int:
    rows: list[dict[str, str]] = []
    with index_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("client") and row.get("messages"):
                rows.append(row)

    if not rows:
        print(f"Нет данных в {index_path}", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: int(r["messages"]), reverse=True)
    emails = [r["client"] for r in rows]
    counts = [r["messages"] for r in rows]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(emails)
        writer.writerow(counts)

    print(f"Клиентов: {len(rows)}")
    print(f"Топ-3: {emails[0]} ({counts[0]}), {emails[1]} ({counts[1]}), {emails[2]} ({counts[2]})")
    print(f"CSV: {output_path.resolve()}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()

    if not args.input.is_file():
        print(f"Нет файла: {args.input}", file=sys.stderr)
        return 1

    return build_from_index(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())

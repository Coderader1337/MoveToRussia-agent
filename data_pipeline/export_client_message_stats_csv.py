"""Пересобрать client_message_stats.csv из index.csv (без IMAP)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_manager_mailboxes import write_client_stats_csv  # noqa: E402

INDEX = ROOT / "mailbox_export" / "index.csv"
OUTPUT = ROOT / "mailbox_export" / "client_message_stats.csv"


def main() -> int:
    if not INDEX.is_file():
        print(f"Нет {INDEX}", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(INDEX.open(encoding="utf-8-sig")))
    write_client_stats_csv(rows, OUTPUT)
    print(f"Записано: {OUTPUT} ({len(rows)} строк)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

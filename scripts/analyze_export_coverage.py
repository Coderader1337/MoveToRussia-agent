"""Локальный анализ покрытия export без IMAP."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "mailbox_export" / "index.csv"
JSONL = ROOT / "mailbox_export" / "all_messages.jsonl"


def main() -> None:
    rows = list(csv.DictReader(INDEX.open(encoding="utf-8-sig")))
    one_way = [r for r in rows if r["two_way"] == "no"]
    two_way = [r for r in rows if r["two_way"] == "yes"]

    print("=== Клиенты (index.csv) ===")
    print(f"Всего контрагентов: {len(rows)}")
    print(f"Двусторонних диалогов: {len(two_way)} ({sum(int(r['messages']) for r in two_way)} писем)")
    print(f"Односторонних (рассылки/уведомления): {len(one_way)} ({sum(int(r['messages']) for r in one_way)} писем)")
    print("\nТоп односторонних (скорее не клиентские переписки):")
    for r in sorted(one_way, key=lambda x: -int(x["messages"]))[:12]:
        print(
            f"  {int(r['messages']):>4}  in={r['incoming']} out={r['outgoing']}  {r['client']}"
        )

    by_mgr = Counter()
    for line in JSONL.open(encoding="utf-8"):
        m = json.loads(line)
        by_mgr[(m["mailbox_account"].split("@")[0], m["direction"])] += 1

    print("\n=== jsonl по менеджерам ===")
    for k, v in sorted(by_mgr.items()):
        print(f"  {k[0]:12} {k[1]:8} {v}")


if __name__ == "__main__":
    main()

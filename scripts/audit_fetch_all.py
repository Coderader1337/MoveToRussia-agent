"""Check fetch gaps per mailbox Sent folder."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from export_manager_mailboxes import (  # noqa: E402
    _env,
    _search_ids,
    _select_mailbox,
    connect_imap,
    fetch_raw_batch,
)
from mail_imap_utils import configured_manager_mailboxes  # noqa: E402

BODY_BATCH = 50


def count_gaps(addr: str, password: str) -> tuple[int, int, int]:
    mail = connect_imap(addr, password)
    try:
        sent_candidates = []
        sent_name = _env("IMAP_SENT_MAILBOX") or ""
        if sent_name:
            sent_candidates.append(sent_name)
        sent_candidates += ["Sent", "Отправленные", "INBOX.Sent", "Sent Items"]
        sent = _select_mailbox(mail, sent_candidates)
        if not sent:
            return 0, 0, 0
        ids = _search_ids(mail, "ALL")
        fetched = 0
        for i in range(0, len(ids), BODY_BATCH):
            chunk = ids[i : i + BODY_BATCH]
            raw_by_seq = fetch_raw_batch(mail, chunk)
            fetched += len(raw_by_seq)
        return len(ids), fetched, len(ids) - fetched
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass


def main() -> int:
    load_dotenv()
    print("Sent FETCH coverage (single session, as in export):\n")
    for addr, key in configured_manager_mailboxes():
        total, got, missing = count_gaps(addr, key)
        pct = 100 * got / total if total else 0
        print(f"{addr}")
        print(f"  SEARCH: {total}, FETCH: {got}, missing: {missing} ({100-pct:.1f}% lost)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

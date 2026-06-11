"""Count IMAP fetch gaps (messages in SEARCH but missing from FETCH)."""
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


def count_fetch_gaps(addr: str, password: str) -> None:
    mail = connect_imap(addr, password)
    try:
        sent_candidates = []
        sent_name = _env("IMAP_SENT_MAILBOX") or ""
        if sent_name:
            sent_candidates.append(sent_name)
        sent_candidates += ["Sent", "Отправленные", "INBOX.Sent", "Sent Items"]
        sent = _select_mailbox(mail, sent_candidates)
        if not sent:
            print(f"Sent not found: {addr}")
            return
        ids = _search_ids(mail, "ALL")
        fetched = 0
        gaps = 0
        batch_errors = 0
        for i in range(0, len(ids), BODY_BATCH):
            chunk = ids[i : i + BODY_BATCH]
            try:
                raw_by_seq = fetch_raw_batch(mail, chunk)
            except Exception:
                batch_errors += 1
                gaps += len(chunk)
                continue
            got = len(raw_by_seq)
            fetched += got
            gaps += len(chunk) - got
            if i % 500 == 0:
                print(f"  ... {i}/{len(ids)} searched, fetched so far {fetched}, gaps {gaps}")
        print(f"\n=== {addr} Sent fetch coverage ===")
        print(f"  SEARCH ids: {len(ids)}")
        print(f"  FETCH got:  {fetched}")
        print(f"  Missing:    {gaps} ({100*gaps/len(ids):.1f}%)")
        print(f"  Batch errors: {batch_errors}")
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
    target = "a.antonova@arkvostok.com"
    for addr, key in configured_manager_mailboxes():
        if addr == target:
            count_fetch_gaps(addr, key)
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

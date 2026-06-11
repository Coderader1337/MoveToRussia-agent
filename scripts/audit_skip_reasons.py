"""Sample skip reasons for messages not in export (read-only IMAP)."""
from __future__ import annotations

import sys
from collections import Counter
from email import message_from_bytes
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
    _header_meta,
    _pick_counterpart,
    _search_ids,
    _select_mailbox,
    connect_imap,
    fetch_raw_batch,
)
from mail_imap_utils import configured_manager_mailboxes, get_text_from_email  # noqa: E402

BODY_BATCH = 50


def audit_account(addr: str, password: str, *, slice_mode: str = "first", max_sent: int = 500) -> None:
    mail = connect_imap(addr, password)
    reasons: Counter[str] = Counter()
    top_recipients: Counter[str] = Counter()
    sample_len = 0
    try:
        sent_candidates = []
        sent_name = _env("IMAP_SENT_MAILBOX") or ""
        if sent_name:
            sent_candidates.append(sent_name)
        sent_candidates += ["Sent", "Отправленные", "INBOX.Sent", "Sent Items"]
        sent = _select_mailbox(mail, sent_candidates)
        if not sent:
            print(f"  Sent not found for {addr}")
            return
        ids = _search_ids(mail, "ALL")
        print(f"  Sent total: {len(ids)}, slice={slice_mode}, up to {max_sent}")
        if slice_mode == "last":
            sample = ids[-max_sent:]
        elif slice_mode == "middle":
            mid = len(ids) // 2
            sample = ids[mid : mid + max_sent]
        else:
            sample = ids[:max_sent]
        sample_len = len(sample)
        for i in range(0, len(sample), BODY_BATCH):
            chunk = sample[i : i + BODY_BATCH]
            raw_by_seq = fetch_raw_batch(mail, chunk)
            for raw in raw_by_seq.values():
                try:
                    msg = message_from_bytes(raw)
                except Exception:
                    reasons["parse_error"] += 1
                    continue
                meta = _header_meta(msg)
                counterpart = _pick_counterpart(
                    "outgoing", meta["from_emails"], meta["recipient_emails"]
                )
                if not counterpart:
                    reasons["no_counterpart"] += 1
                    for r in meta["recipient_emails"]:
                        top_recipients[r] += 1
                    continue
                text = get_text_from_email(msg)
                if not text.strip():
                    reasons["empty_body"] += 1
                    continue
                reasons["would_export"] += 1
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass

    print(f"\n=== {addr} Sent [{slice_mode}] sample ({sample_len} msgs) ===")
    for k, v in reasons.most_common():
        print(f"  {k}: {v}")
    print("  Top TO (no_counterpart):")
    for r, c in top_recipients.most_common(10):
        print(f"    {r}: {c}")


def main() -> int:
    load_dotenv()
    target = "a.antonova@arkvostok.com"
    for addr, key in configured_manager_mailboxes():
        if addr == target:
            for mode in ("first", "middle", "last"):
                audit_account(addr, key, slice_mode=mode, max_sent=800)
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

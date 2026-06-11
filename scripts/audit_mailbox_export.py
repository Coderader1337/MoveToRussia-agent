"""
Аудит полноты выгрузки mailbox_export vs IMAP (read-only).

Сравнивает:
  - сколько писем в ящиках по IMAP (ALL / SEEN / UNSEEN в INBOX, ALL в Sent)
  - сколько попало в all_messages.jsonl
  - почему письма отбрасываются (нет контрагента, пустое тело, дедуп)

  python audit_mailbox_export.py
"""

from __future__ import annotations

import imaplib
import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "mailbox_export" / "all_messages.jsonl"

from export_manager_mailboxes import (  # noqa: E402
    _env,
    _search_ids,
    _select_mailbox,
    connect_imap,
)
from mail_imap_utils import configured_manager_mailboxes  # noqa: E402


def imap_counts(manager_email: str, manager_password: str) -> dict[str, int]:
    mail = connect_imap(manager_email, manager_password)
    out: dict[str, int] = {}
    try:
        if _select_mailbox(mail, ["INBOX"]):
            out["inbox_all"] = len(_search_ids(mail, "ALL"))
            out["inbox_seen"] = len(_search_ids(mail, "SEEN"))
            out["inbox_unseen"] = len(_search_ids(mail, "UNSEEN"))
        sent_candidates = []
        sent_name = _env("IMAP_SENT_MAILBOX") or ""
        if sent_name:
            sent_candidates.append(sent_name)
        sent_candidates += ["Sent", "Отправленные", "INBOX.Sent", "Sent Items"]
        sent = _select_mailbox(mail, sent_candidates)
        if sent:
            out["sent_all"] = len(_search_ids(mail, "ALL"))
        out["sent_folder"] = sent or ""
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
    return out


def load_export_stats() -> dict[str, Counter]:
    by_account: Counter[str] = Counter()
    by_folder: Counter[str] = Counter()
    if not JSONL.is_file():
        return {"by_account": by_account, "by_folder": by_folder}
    with JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            by_account[rec.get("mailbox_account", "?")] += 1
            key = f"{rec.get('mailbox_account','?')}/{rec.get('mailbox_folder','?')}/{rec.get('direction','?')}"
            by_folder[key] += 1
    return {"by_account": by_account, "by_folder": by_folder}


def main() -> int:
    load_dotenv()
    if not JSONL.is_file():
        print(f"Нет {JSONL} — сначала export_manager_mailboxes.py", file=sys.stderr)
        return 1

    exp = load_export_stats()
    total_export = sum(exp["by_account"].values())
    print(f"Экспорт (jsonl): {total_export} писем\n")

    grand = {
        "inbox_all": 0,
        "inbox_seen": 0,
        "inbox_unseen": 0,
        "sent_all": 0,
        "exported": 0,
    }

    for addr, key in configured_manager_mailboxes():
        print(f"=== {addr} ===")
        imap = imap_counts(addr, key)
        exported = exp["by_account"].get(addr, 0)
        inbox_seen = imap.get("inbox_seen", 0)
        inbox_unseen = imap.get("inbox_unseen", 0)
        inbox_all = imap.get("inbox_all", 0)
        sent_all = imap.get("sent_all", 0)

        # Текущая логика выгрузки: SEEN из INBOX + ALL из Sent
        export_scope = inbox_seen + sent_all
        gap = export_scope - exported

        print(f"  INBOX: всего {inbox_all} | прочит. {inbox_seen} | непрочит. {inbox_unseen}")
        print(f"  Sent ({imap.get('sent_folder')}): {sent_all}")
        print(f"  Охват выгрузки (SEEN+Sent): {export_scope}")
        print(f"  В jsonl: {exported}")
        print(f"  Не попало в jsonl (фильтры/пропуски): ~{gap}")
        if inbox_unseen:
            print(f"  ⚠ Непрочитанных входящих НЕ выгружается по текущим правилам: {inbox_unseen}")

        for k, v in sorted(exp["by_folder"].items()):
            if k.startswith(addr):
                print(f"    jsonl {k}: {v}")

        for gk in ("inbox_all", "inbox_seen", "inbox_unseen", "sent_all", "exported"):
            if gk == "exported":
                grand[gk] += exported
            else:
                grand[gk] += imap.get(gk, 0)

        print()

    print("=== ИТОГО (3 ящика) ===")
    scope = grand["inbox_seen"] + grand["sent_all"]
    print(f"INBOX прочитанных: {grand['inbox_seen']}")
    print(f"INBOX непрочитанных (не в выгрузке): {grand['inbox_unseen']}")
    print(f"Sent: {grand['sent_all']}")
    print(f"Потенциальный охват (SEEN+Sent): {scope}")
    print(f"Фактически в jsonl: {grand['exported']}")
    print(f"Отфильтровано/пропущено: ~{scope - grand['exported']}")
    pct = 100 * grand["exported"] / scope if scope else 0
    print(f"Доля захвата от SEEN+Sent: {pct:.1f}%")

    # Типичные причины из последней выгрузки (из логов)
    print("\n=== Известные причины потерь ===")
    print("1. INBOX: только SEEN — непрочитанные входящие не берутся (по ТЗ)")
    print("2. Пустое тело (только вложения / HTML без текста)")
    print("3. Нет внешнего контрагента (служебные, noreply, внутренние)")
    print("4. Дедупликация между ящиками (~28 писем)")
    print("5. Другие папки (Spam, Trash, Archive) не сканируются")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

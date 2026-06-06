"""
Массовая выгрузка переписок менеджеров с клиентами (read-only IMAP, Yandex).

Берёт из ящиков e.novik и a.antonova:
  * ВСЕ исходящие письма (папка Sent / Отправленные);
  * ВСЕ прочитанные (\\Seen) входящие письма (INBOX).

Письма группируются по клиенту (внешний контрагент, не @arkvostok.com),
сортируются хронологически и складываются в отдельную папку mailbox_export/:
  threads/<client>.txt   — единая хронологическая переписка по клиенту
  all_messages.jsonl     — все письма построчным JSON (вход для анализа)
  index.csv              — сводка по клиентам
  summary.json           — общая статистика выгрузки

Только чтение: EXAMINE (readonly=True) + BODY.PEEK[] (не ставит \\Seen).

.env:
  E_NOVIK_MAIL_ADRESS, E_NOVIK_MAIL_KEY
  A_ANTONOVA_MAIL_ADRESS, A_ANTONOVA_MAIL_KEY
  IMAP_SENT_MAILBOX — опционально (имя папки исходящих, если не Sent)

Примеры:
  python export_manager_mailboxes.py
  python export_manager_mailboxes.py --limit 50          # для теста: <=50 писем на папку
  python export_manager_mailboxes.py --since 01-Jan-2024 # только письма с даты
"""

from __future__ import annotations

import argparse
import csv
import imaplib
import json
import re
import sys
import time
from datetime import datetime
from email import message_from_bytes
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from mail_imap_utils import (
    _env,
    configured_manager_mailboxes,
    connect_imap,
    decode_mime_words,
    get_text_from_email,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "mailbox_export"
THREADS_DIR = OUTPUT_DIR / "threads"

INTERNAL_DOMAIN = "arkvostok.com"

# Локальные части писем-роботов / служебных адресов: не считаем клиентами.
AUTOMATED_LOCALPARTS = re.compile(
    r"(no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon|postmaster|bounce|"
    r"newsletter|notifications?|mailer|daemon|abuse|robot|noreply)",
    re.I,
)
# Домены массовых рассылок/служб, которые точно не клиенты.
AUTOMATED_DOMAINS = re.compile(
    r"(yandex-team|sendpulse|unisender|mailchimp|sendgrid|amazonses|"
    r"notify\.|mail\.yandex|google\.com$|accounts\.google)",
    re.I,
)

BODY_BATCH = 50

MAIL_SEPARATOR = "=" * 78
INNER_RULE = "-" * 78


def _emails_from(*header_values: str) -> list[str]:
    parsed = getaddresses([h for h in header_values if h])
    out: list[str] = []
    seen: set[str] = set()
    for _, addr in parsed:
        a = addr.strip().lower()
        if a and "@" in a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _is_internal(addr: str) -> bool:
    return addr.lower().endswith("@" + INTERNAL_DOMAIN)


def _is_automated(addr: str) -> bool:
    local = addr.split("@", 1)[0]
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    return bool(AUTOMATED_LOCALPARTS.search(local)) or bool(
        AUTOMATED_DOMAINS.search(domain)
    )


def _pick_counterpart(
    direction: str, from_emails: list[str], recipient_emails: list[str]
) -> str:
    """Внешний контрагент (клиент). Для исходящих — получатель, для входящих — отправитель."""
    pool = recipient_emails if direction == "outgoing" else from_emails
    for a in pool:
        if not _is_internal(a) and not _is_automated(a):
            return a
    # запасной вариант: любой внешний адрес из всех заголовков
    for a in from_emails + recipient_emails:
        if not _is_internal(a) and not _is_automated(a):
            return a
    return ""


def _parse_ts(date_str: str) -> float:
    if not date_str:
        return 0.0
    try:
        return parsedate_to_datetime(date_str).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _select_mailbox(mail: imaplib.IMAP4_SSL, candidates: list[str]) -> str | None:
    for name in candidates:
        try:
            status, _ = mail.select(name, readonly=True)
            if status == "OK":
                return name
        except imaplib.IMAP4.error:
            continue
    return None


def _search_ids(mail: imaplib.IMAP4_SSL, criterion: str) -> list[bytes]:
    status, data = mail.search(None, criterion)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _iter_fetch_tuples(data: list[Any]):
    """Из ответа FETCH достаёт пары (seq_id:int, raw:bytes) для батч-запросов."""
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        head = item[0]
        body = item[1]
        if not isinstance(body, (bytes, bytearray)):
            continue
        m = re.match(rb"^\s*(\d+)\s", head if isinstance(head, bytes) else b"")
        seq = int(m.group(1)) if m else -1
        yield seq, bytes(body)


def fetch_raw_batch(mail: imaplib.IMAP4_SSL, ids: list[bytes]) -> dict[int, bytes]:
    """seq_id -> сырое письмо (BODY.PEEK[], батчами для скорости)."""
    out: dict[int, bytes] = {}
    for i in range(0, len(ids), BODY_BATCH):
        chunk = ids[i : i + BODY_BATCH]
        idset = b",".join(chunk)
        try:
            status, data = mail.fetch(idset, "(BODY.PEEK[])")
        except imaplib.IMAP4.error:
            continue
        if status != "OK" or not data:
            continue
        for seq, raw in _iter_fetch_tuples(data):
            if seq > 0 and len(raw) > 40:
                out[seq] = raw
    return out


def _header_meta(msg: Any) -> dict[str, Any]:
    from_h = decode_mime_words(msg.get("From")) or ""
    to_h = decode_mime_words(msg.get("To")) or ""
    cc_h = decode_mime_words(msg.get("Cc")) or ""
    return {
        "subject": decode_mime_words(msg.get("Subject")) or "",
        "from": from_h,
        "to": to_h,
        "cc": cc_h,
        "date": msg.get("Date") or "",
        "message_id": (msg.get("Message-ID") or "").strip(),
        "from_emails": _emails_from(from_h),
        "recipient_emails": _emails_from(to_h, cc_h),
    }


def collect_from_account(
    manager_email: str,
    manager_password: str,
    *,
    limit: int | None,
    since: str | None,
    sink: list[dict[str, Any]],
) -> dict[str, int]:
    """Собирает письма в общий список ``sink`` (устойчиво к обрыву сессии)."""
    stats = {"inbox_seen": 0, "sent_total": 0, "skipped": 0, "fetched": 0}
    messages = sink
    mail = connect_imap(manager_email, manager_password)
    try:
        sent_name = _env("IMAP_SENT_MAILBOX") or ""
        sent_candidates = [sent_name] if sent_name else []
        sent_candidates += ["Sent", "Отправленные", "INBOX.Sent", "Sent Items"]

        plan = [
            ("INBOX", "incoming", f'(SEEN{(" SINCE " + since) if since else ""})'),
        ]

        # INBOX (прочитанные входящие)
        for mailbox, direction, criterion in plan:
            selected = _select_mailbox(mail, [mailbox])
            if not selected:
                print(f"  [{manager_email}] папка {mailbox} недоступна", file=sys.stderr)
                continue
            ids = _search_ids(mail, criterion)
            if direction == "incoming":
                stats["inbox_seen"] = len(ids)
            if limit:
                ids = ids[-limit:]
            messages += _collect_messages(
                mail, ids, direction, manager_email, selected, stats
            )

        # Sent (все исходящие)
        sent_selected = _select_mailbox(mail, sent_candidates)
        if sent_selected:
            crit = f"(SINCE {since})" if since else "ALL"
            ids = _search_ids(mail, crit)
            stats["sent_total"] = len(ids)
            if limit:
                ids = ids[-limit:]
            messages += _collect_messages(
                mail, ids, "outgoing", manager_email, sent_selected, stats
            )
        else:
            print(f"  [{manager_email}] папка исходящих не найдена", file=sys.stderr)
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
    return stats


def _collect_messages(
    mail: imaplib.IMAP4_SSL,
    ids: list[bytes],
    direction: str,
    manager_email: str,
    mailbox_label: str,
    stats: dict[str, int],
) -> list[dict[str, Any]]:
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    total = len(ids)
    done = 0
    for i in range(0, len(ids), BODY_BATCH):
        chunk = ids[i : i + BODY_BATCH]
        raw_by_seq = fetch_raw_batch(mail, chunk)
        for seq, raw in raw_by_seq.items():
            done += 1
            try:
                msg = message_from_bytes(raw)
            except Exception:
                stats["skipped"] += 1
                continue
            stats["fetched"] += 1
            meta = _header_meta(msg)
            counterpart = _pick_counterpart(
                direction, meta["from_emails"], meta["recipient_emails"]
            )
            if not counterpart:
                stats["skipped"] += 1
                continue
            text = get_text_from_email(msg)
            if not text.strip():
                stats["skipped"] += 1
                continue
            out.append(
                {
                    "mailbox_account": manager_email,
                    "mailbox_folder": mailbox_label,
                    "direction": direction,
                    "counterpart": counterpart,
                    "subject": meta["subject"],
                    "from": meta["from"],
                    "to": meta["to"],
                    "cc": meta["cc"],
                    "date": meta["date"],
                    "message_id": meta["message_id"],
                    "text": text,
                    "_ts": _parse_ts(meta["date"]),
                }
            )
        print(
            f"    [{manager_email}/{mailbox_label}/{direction}] "
            f"{min(done, total)}/{total} (собрано {len(out)})"
        )
    return out


def dedupe(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_mid: set[str] = set()
    seen_fallback: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for m in messages:
        mid = m.get("message_id") or ""
        if mid:
            if mid in seen_mid:
                continue
            seen_mid.add(mid)
        else:
            key = (m.get("from"), m.get("date"), (m.get("text") or "")[:120])
            if key in seen_fallback:
                continue
            seen_fallback.add(key)
        out.append(m)
    return out


def _safe_name(email_addr: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", email_addr.strip().lower()).strip("_")


def write_thread_file(client: str, msgs: list[dict[str, Any]]) -> Path:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    path = THREADS_DIR / f"{_safe_name(client)}.txt"
    parts: list[str] = [
        f"КЛИЕНТ: {client}",
        f"ПИСЕМ: {len(msgs)}",
        "",
    ]
    for m in msgs:
        arrow = "МЕНЕДЖЕР → КЛИЕНТ" if m["direction"] == "outgoing" else "КЛИЕНТ → МЕНЕДЖЕР"
        parts.append(MAIL_SEPARATOR)
        parts.append(f"[{arrow}]  ({m.get('mailbox_account')})")
        parts.append(f"Тема: {m.get('subject') or '—'}")
        parts.append(f"От: {m.get('from') or '—'}")
        parts.append(f"Кому: {m.get('to') or '—'}")
        parts.append(f"Дата: {m.get('date') or '—'}")
        parts.append(INNER_RULE)
        parts.append((m.get("text") or "").strip() or "(пустое тело)")
        parts.append("")
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


def write_outputs(messages: list[dict[str, Any]]) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # all_messages.jsonl
    jsonl_path = OUTPUT_DIR / "all_messages.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as f:
        for m in sorted(messages, key=lambda x: (x["counterpart"], x["_ts"])):
            rec = {k: v for k, v in m.items() if k != "_ts"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # группировка по клиентам
    by_client: dict[str, list[dict[str, Any]]] = {}
    for m in messages:
        by_client.setdefault(m["counterpart"], []).append(m)

    index_rows: list[dict[str, Any]] = []
    for client, msgs in by_client.items():
        msgs.sort(key=lambda x: x["_ts"])
        write_thread_file(client, msgs)
        n_out = sum(1 for x in msgs if x["direction"] == "outgoing")
        n_in = len(msgs) - n_out
        ts_vals = [x["_ts"] for x in msgs if x["_ts"]]
        first = (
            datetime.fromtimestamp(min(ts_vals)).strftime("%Y-%m-%d") if ts_vals else ""
        )
        last = (
            datetime.fromtimestamp(max(ts_vals)).strftime("%Y-%m-%d") if ts_vals else ""
        )
        index_rows.append(
            {
                "client": client,
                "messages": len(msgs),
                "outgoing": n_out,
                "incoming": n_in,
                "first_date": first,
                "last_date": last,
                "two_way": "yes" if (n_out and n_in) else "no",
            }
        )

    index_rows.sort(key=lambda r: r["messages"], reverse=True)
    index_path = OUTPUT_DIR / "index.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "client",
                "messages",
                "outgoing",
                "incoming",
                "first_date",
                "last_date",
                "two_way",
            ],
        )
        writer.writeheader()
        writer.writerows(index_rows)

    summary = {
        "total_messages": len(messages),
        "total_clients": len(by_client),
        "two_way_clients": sum(1 for r in index_rows if r["two_way"] == "yes"),
        "outgoing": sum(1 for m in messages if m["direction"] == "outgoing"),
        "incoming": sum(1 for m in messages if m["direction"] == "incoming"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Не более N последних писем на папку (для теста)",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help='Только письма с даты (IMAP-формат, напр. "01-Jan-2024")',
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    mailboxes = configured_manager_mailboxes()
    if not mailboxes:
        print(
            "Нужны E_NOVIK_MAIL_ADRESS/E_NOVIK_MAIL_KEY и/или "
            "A_ANTONOVA_MAIL_ADRESS/A_ANTONOVA_MAIL_KEY в .env",
            file=sys.stderr,
        )
        return 1

    print(f"Ящиков к выгрузке: {len(mailboxes)}")
    t0 = time.time()
    all_messages: list[dict[str, Any]] = []
    for addr, key in mailboxes:
        print(f"\n=== {addr} ===")
        before = len(all_messages)
        try:
            stats = collect_from_account(
                addr, key, limit=args.limit, since=args.since, sink=all_messages
            )
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"  Сессия {addr} прервана: {exc} (частичные данные сохранены)",
                  file=sys.stderr)
            stats = {"inbox_seen": 0, "sent_total": 0, "skipped": 0, "fetched": 0}
        print(
            f"  INBOX(прочит.): {stats['inbox_seen']} | Sent: {stats['sent_total']} | "
            f"тел загружено: {stats['fetched']} | пропущено: {stats['skipped']} | "
            f"в выборке: {len(all_messages) - before}"
        )

    before = len(all_messages)
    all_messages = dedupe(all_messages)
    print(f"\nДедупликация: {before} -> {len(all_messages)}")

    summary = write_outputs(all_messages)
    print(f"\nГотово за {time.time() - t0:.0f} c")
    print(f"  Папка: {OUTPUT_DIR}")
    print(
        f"  Писем: {summary['total_messages']} | "
        f"Клиентов: {summary['total_clients']} | "
        f"Двусторонних диалогов: {summary['two_way_clients']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
IMAP Yandex: первое исходящее письмо менеджера клиенту (read-only).

Ищет в ящиках e.novik и a.antonova (или legacy MAIL_ADRESS), папка Sent.
Берёт самое раннее по дате письмо «от менеджера → клиенту».

.env:
  E_NOVIK_MAIL_ADRESS, E_NOVIK_MAIL_KEY
  A_ANTONOVA_MAIL_ADRESS, A_ANTONOVA_MAIL_KEY
  IMAP_SENT_MAILBOX — опционально (по умолчанию Sent)
"""

from __future__ import annotations

import email
import html
import imaplib
import os
import re
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993

_MANAGER_MAILBOX_ENV = (
    ("E_NOVIK_MAIL_ADRESS", "E_NOVIK_MAIL_KEY"),
    ("A_ANTONOVA_MAIL_ADRESS", "A_ANTONOVA_MAIL_KEY"),
)


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def configured_manager_mailboxes() -> list[tuple[str, str]]:
    """(email, app_password) для каждого настроенного ящика менеджера."""
    boxes: list[tuple[str, str]] = []
    for addr_key, pass_key in _MANAGER_MAILBOX_ENV:
        addr = _env(addr_key)
        key = _env(pass_key)
        if addr and key:
            boxes.append((addr, key))
    if not boxes:
        addr = _env("MAIL_ADRESS")
        key = _env("MAIL_KEY")
        if addr and key:
            boxes.append((addr, key))
    return boxes


def decode_mime_words(s: str | None) -> str:
    if s is None:
        return ""
    decoded_fragments = decode_header(s)
    return "".join(
        str(t, enc or "utf-8") if isinstance(t, bytes) else str(t)
        for t, enc in decoded_fragments
    )


def _decode_part_payload(part: email.message.Message) -> str:
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    except Exception:
        return ""


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_quoted_reply_text(value: str) -> str:
    if not value:
        return ""
    text = value.replace("\r", "").strip()
    split_patterns = [
        r"(?im)^\s*On .+wrote:\s*$",
        r"(?im)^\s*В .+писал\(а\):\s*$",
        r"(?im)^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
        r"(?im)^\s*El .+escribió:\s*$",
    ]
    cut_positions = [
        m.start()
        for p in split_patterns
        for m in [re.search(p, text)]
        if m
    ]
    # Цитата в формате Яндекс.Почты: ---------------- + Кому:/Тема:
    yandex = re.search(r"\n\s*-{8,}\s*\n\s*(?:Кому:|To:|Тема:|Subject:)", text, re.I)
    if yandex:
        cut_positions.append(yandex.start())
    if cut_positions:
        text = text[: min(cut_positions)].rstrip()
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if re.match(r"^\s*>+", line):
            break
        cleaned.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def get_text_from_email(msg: email.message.Message) -> str:
    disp_attachment = re.compile(r"attachment", re.I)
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if disp_attachment.search(str(part.get("Content-Disposition", ""))):
                continue
            block = _decode_part_payload(part).strip()
            if not block:
                continue
            if part.get_content_type() == "text/plain":
                plain_chunks.append(block)
            elif part.get_content_type() == "text/html":
                html_chunks.append(_html_to_text(block))
        if plain_chunks:
            return _strip_quoted_reply_text("\n\n".join(plain_chunks).strip())
        if html_chunks:
            return _strip_quoted_reply_text("\n\n".join(html_chunks).strip())
        return ""

    if msg.get_content_type() == "text/plain":
        return _strip_quoted_reply_text(_decode_part_payload(msg).strip())
    if msg.get_content_type() == "text/html":
        return _strip_quoted_reply_text(_html_to_text(_decode_part_payload(msg)))
    return ""


def _imap_quote_addr(addr: str) -> str:
    return addr.replace("\\", "\\\\").replace('"', '\\"')


def _raw_message_from_fetch(msg_data: list[Any] | None) -> bytes | None:
    if not msg_data:
        return None
    candidates: list[bytes] = []
    for item in msg_data:
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, (bytes, bytearray)):
                    candidates.append(bytes(part))
        elif isinstance(item, (bytes, bytearray)):
            candidates.append(bytes(item))
    if not candidates:
        return None
    return max(candidates, key=len)


def _extract_header_emails(*header_values: str) -> set[str]:
    parsed = getaddresses(header_values)
    return {addr.strip().lower() for _, addr in parsed if addr.strip()}


def _is_manager_to_client_sent(
    manager_email: str,
    client_email: str,
    from_header: str,
    to_header: str,
    cc_header: str,
) -> bool:
    manager = manager_email.strip().lower()
    client = client_email.strip().lower()
    from_emails = _extract_header_emails(from_header)
    recipients = _extract_header_emails(to_header, cc_header)
    return manager in from_emails and client in recipients


def connect_imap(manager_email: str, manager_password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(manager_email, manager_password)
    return mail


def search_email_ids(
    mail: imaplib.IMAP4_SSL, contact_email: str, mailbox: str
) -> list[bytes]:
    mail.select(mailbox, readonly=True)
    q = _imap_quote_addr(contact_email.strip())
    found: set[bytes] = set()
    for crit in (f'FROM "{q}"', f'TO "{q}"', f'CC "{q}"'):
        status, messages = mail.search(None, crit)
        if status == "OK" and messages and messages[0]:
            found.update(messages[0].split())
    return list(found)


def _fetch_sent_to_client(
    mail: imaplib.IMAP4_SSL,
    email_ids: list[bytes],
    manager_email: str,
    client_email: str,
    mailbox_account: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for eid in email_ids:
        if eid in seen:
            continue
        seen.add(eid)
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data:
            continue
        raw = _raw_message_from_fetch(msg_data)
        if not raw:
            continue
        msg = email.message_from_bytes(raw)
        from_header = decode_mime_words(msg["From"]) or ""
        to_header = decode_mime_words(msg["To"]) or ""
        cc_header = decode_mime_words(msg["Cc"]) or ""
        if not _is_manager_to_client_sent(
            manager_email, client_email, from_header, to_header, cc_header
        ):
            continue
        date_str = msg["Date"] or ""
        ts = 0.0
        if date_str:
            try:
                ts = parsedate_to_datetime(date_str).timestamp()
            except (TypeError, ValueError, OverflowError):
                ts = 0.0
        text = get_text_from_email(msg)
        if not text.strip():
            continue
        out.append(
            {
                "subject": decode_mime_words(msg["Subject"]) or "",
                "from": from_header,
                "to": to_header,
                "date": date_str,
                "text": text,
                "_ts": ts,
                "mailbox_account": mailbox_account,
            }
        )
    return out


def _collect_sent_from_account(
    manager_email: str,
    manager_password: str,
    client_email: str,
    sent_mailbox: str,
) -> list[dict[str, Any]]:
    mail = connect_imap(manager_email, manager_password)
    try:
        ids = search_email_ids(mail, client_email, sent_mailbox)
        return _fetch_sent_to_client(
            mail, ids, manager_email, client_email, manager_email
        )
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()


def format_email_block(item: dict[str, Any]) -> str:
    parts = [
        f"Ящик: {item.get('mailbox_account') or '—'}",
        f"Тема: {item.get('subject') or '—'}",
        f"От: {item.get('from') or '—'}",
        f"Кому: {item.get('to') or '—'}",
        f"Дата: {item.get('date') or '—'}",
        "-" * 40,
        (item.get("text") or "").strip(),
    ]
    return "\n".join(parts).strip()


def fetch_first_manager_email_to_client(client_email: str) -> str:
    """
    Самое раннее исходящее письмо клиенту среди всех настроенных ящиков менеджеров.
    Read-only IMAP (EXAMINE, BODY.PEEK[]).
    """
    mailboxes = configured_manager_mailboxes()
    if not mailboxes:
        raise RuntimeError(
            "Нужны E_NOVIK_MAIL_ADRESS/E_NOVIK_MAIL_KEY и/или "
            "A_ANTONOVA_MAIL_ADRESS/A_ANTONOVA_MAIL_KEY в .env"
        )

    sent_mailbox = _env("IMAP_SENT_MAILBOX") or "Sent"
    all_items: list[dict[str, Any]] = []

    for manager_email, manager_password in mailboxes:
        try:
            items = _collect_sent_from_account(
                manager_email, manager_password, client_email, sent_mailbox
            )
            all_items.extend(items)
        except (UnicodeEncodeError, imaplib.IMAP4.error, OSError):
            continue

    if not all_items:
        return ""

    with_ts = [x for x in all_items if float(x.get("_ts") or 0) > 0]
    pool = with_ts if with_ts else all_items
    pool.sort(key=lambda x: float(x.get("_ts") or 0))
    return format_email_block(pool[0])

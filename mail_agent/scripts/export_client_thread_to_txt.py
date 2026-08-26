"""
Переписка менеджера с клиентом в один текстовый файл (хронологический порядок).

IMAP как в export_client_context_to_sheets.py (только почта, без CRM и Sheets):
  MAIL_ADRESS, MAIL_KEY — ящик менеджера (Yandex)
  CLIENT_EMAIL — опционально
  IMAP_SENT_MAILBOX — опционально (если папка исходящих не Sent)

В файле: тема, от, к кому, тело (text/plain). Письма разделены линиями.

Примеры:
  python export_client_thread_to_txt.py
  python export_client_thread_to_txt.py client@example.com
  python export_client_thread_to_txt.py -o my_thread.txt client@example.com
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993
OUTPUT_DIR = Path("exported_emails_txt")
DEFAULT_CLIENT_EMAIL = "client@example.com"

MAIL_SEPARATOR = "=" * 78
INNER_RULE = "-" * 78


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def _post_json(url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        code = e.code
    try:
        parsed: Any = json.loads(body) if body.strip() else None
    except Exception:
        parsed = body
    return code, parsed


def fetch_crm(client_email: str) -> dict[str, Any]:
    base = _env("ENVYCRM_BASE_URL").rstrip("/")
    api_key = _env("ENVYCRM_KEY")
    if not base or not api_key:
        return {}
    q = urllib.parse.urlencode({"api_key": api_key})
    body = {"request": {"email": client_email}}
    out: dict[str, Any] = {"email": client_email, "results": {}}
    for name, path in (
        ("lead_search", f"{base}/crm/api/v1/lead/search/?{q}"),
        ("deal_search", f"{base}/crm/api/v1/deal/search/?{q}"),
    ):
        code, parsed = _post_json(path, body)
        out["results"][name] = {"http_status": code, "body": parsed}
    return out


def _crm_base_and_key() -> tuple[str, str]:
    return _env("ENVYCRM_BASE_URL").rstrip("/"), _env("ENVYCRM_KEY")


def _post_crm(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    base, api_key = _crm_base_and_key()
    if not base or not api_key:
        return 0, None
    q = urllib.parse.urlencode({"api_key": api_key})
    return _post_json(f"{base}{path}?{q}", payload)


def _extract_ids_from_crm(crm: dict[str, Any]) -> tuple[list[int], list[int]]:
    deal_ids: set[int] = set()
    client_ids: set[int] = set()
    clients = (
        crm.get("results", {})
        .get("deal_search", {})
        .get("body", {})
        .get("clients", [])
    )
    if isinstance(clients, list):
        for cl in clients:
            if not isinstance(cl, dict):
                continue
            cid = cl.get("id")
            if isinstance(cid, int):
                client_ids.add(cid)
            deals = cl.get("deals_for_event", [])
            if isinstance(deals, list):
                for d in deals:
                    if isinstance(d, dict) and isinstance(d.get("id"), int):
                        deal_ids.add(int(d["id"]))
    return sorted(deal_ids), sorted(client_ids)


def _fetch_crm_details_for_question(
    deal_ids: list[int], client_ids: list[int]
) -> list[dict[str, Any]]:
    """
    Дополнительные запросы в CRM: в карточках клиента/сделки часто лежит «Вопрос клиента».
    """
    payloads: list[dict[str, Any]] = []
    for did in deal_ids:
        for key in ("id", "deal_id"):
            for path in ("/crm/api/v1/deal/get/", "/crm/api/v1/deal/search/"):
                code, parsed = _post_crm(path, {"request": {key: did}})
                if code and parsed:
                    payloads.append({"path": path, "code": code, "body": parsed})
    for cid in client_ids:
        for key in ("id", "client_id"):
            for path in ("/crm/api/v1/client/get/", "/crm/api/v1/client/search/"):
                code, parsed = _post_crm(path, {"request": {key: cid}})
                if code and parsed:
                    payloads.append({"path": path, "code": code, "body": parsed})
    return payloads


def _extract_client_question_from_crm(crm: dict[str, Any]) -> str:
    """
    Находит поле «Вопрос клиента» / Client Question в произвольной вложенности CRM-ответа.
    """
    key_markers = (
        "вопрос клиента",
        "client question",
        "client_question",
        "question_client",
        "question from client",
        "вопрос",
    )
    bad_values = {"", "success", "ok", "none", "null"}

    candidates: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            # Частый формат кастомных полей: {"name": "...", "value": "..."}
            nm = str(node.get("name", "")).strip().lower()
            if nm and any(m in nm for m in key_markers):
                val = str(node.get("value", "")).strip()
                if val and val.lower() not in bad_values:
                    candidates.append(val)

            for k, v in node.items():
                k_norm = str(k).strip().lower()
                if any(m in k_norm for m in key_markers):
                    if isinstance(v, str):
                        vv = v.strip()
                        if vv and vv.lower() not in bad_values:
                            candidates.append(vv)
                    elif isinstance(v, (dict, list)):
                        walk(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(crm)
    if not candidates:
        return ""
    # Обычно вопрос клиента — самый длинный осмысленный текст среди совпадений.
    return max(candidates, key=lambda s: len(s.strip())).strip()


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


def get_text_from_email(msg: email.message.Message) -> str:
    """Только text/plain из MIME (без HTML)."""
    disp_attachment = re.compile(r"attachment", re.I)
    chunks: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() != "text/plain":
                continue
            if disp_attachment.search(str(part.get("Content-Disposition", ""))):
                continue
            block = _decode_part_payload(part).strip()
            if block:
                chunks.append(block)
        return "\n\n".join(chunks).strip()

    if msg.get_content_type() == "text/plain":
        return _decode_part_payload(msg).strip()
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


def search_email_ids(
    mail: imaplib.IMAP4_SSL, contact_email: str, mailbox: str = "INBOX"
) -> list[bytes]:
    mail.select(mailbox, readonly=True)
    q = _imap_quote_addr(contact_email.strip())
    found: set[bytes] = set()
    for crit in (f'FROM "{q}"', f'TO "{q}"', f'CC "{q}"'):
        status, messages = mail.search(None, crit)
        if status == "OK" and messages and messages[0]:
            found.update(messages[0].split())
    return list(found)


def fetch_emails_by_ids(
    mail: imaplib.IMAP4_SSL,
    email_ids: list[bytes],
    folder_label: str = "",
) -> list[dict[str, Any]]:
    seen: set[bytes] = set()
    out: list[dict[str, Any]] = []
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
        subject = decode_mime_words(msg["Subject"]) or "Без темы"
        from_header = decode_mime_words(msg["From"]) or ""
        to_header = decode_mime_words(msg["To"]) or ""
        date_str = msg["Date"] or ""
        text_plain = get_text_from_email(msg)
        ts = 0.0
        if date_str:
            try:
                ts = parsedate_to_datetime(date_str).timestamp()
            except (TypeError, ValueError, OverflowError):
                ts = 0.0
        out.append(
            {
                "folder": folder_label,
                "subject": subject,
                "from": from_header,
                "to": to_header,
                "date": date_str,
                "textPlain": text_plain,
                "_ts": ts,
            }
        )
    return out


def connect_imap() -> imaplib.IMAP4_SSL:
    addr = _env("MAIL_ADRESS")
    key = _env("MAIL_KEY")
    if not addr or not key:
        raise RuntimeError("Нужны MAIL_ADRESS и MAIL_KEY в .env")
    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(addr, key)
    return m


def _collect_emails(client_email: str) -> list[dict[str, Any]]:
    mail = connect_imap()
    try:
        emails: list[dict[str, Any]] = []
        ids_in = search_email_ids(mail, client_email, "INBOX")
        emails.extend(fetch_emails_by_ids(mail, ids_in, "INBOX"))

        sent_mailbox = _env("IMAP_SENT_MAILBOX") or "Sent"
        try:
            ids_out = search_email_ids(mail, client_email, sent_mailbox)
            emails.extend(fetch_emails_by_ids(mail, ids_out, sent_mailbox))
        except (UnicodeEncodeError, imaplib.IMAP4.error) as e:
            print(
                f"IMAP: не удалось открыть папку исходящих «{sent_mailbox}»: {e}",
                file=sys.stderr,
            )

        emails.sort(key=lambda x: float(x.get("_ts", 0.0)))
        for item in emails:
            item.pop("_ts", None)
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()
    return emails


def _format_one_mail(
    subject: str,
    from_addr: str,
    to_addr: str,
    body: str,
) -> str:
    body = (body or "").strip()
    lines = [
        MAIL_SEPARATOR,
        f"Тема: {subject}",
        f"От: {from_addr}",
        f"Кому: {to_addr}",
        INNER_RULE,
        body if body else "(текст письма отсутствует или только HTML)",
        "",
    ]
    return "\n".join(lines)


def build_thread_text(
    emails: list[dict[str, Any]],
    client_question: str = "",
    client_email: str = "",
    manager_email: str = "",
) -> str:
    parts: list[str] = []
    if client_question.strip():
        parts.append(
            _format_one_mail(
                "Первичное обращение с сайта (CRM: Вопрос клиента)",
                client_email.strip(),
                manager_email.strip(),
                client_question.strip(),
            )
        )
    for e in emails:
        parts.append(
            _format_one_mail(
                str(e.get("subject") or "").strip() or "Без темы",
                str(e.get("from") or "").strip(),
                str(e.get("to") or "").strip(),
                str(e.get("textPlain") or ""),
            )
        )
    return "\n".join(parts).rstrip() + "\n"


def _default_out_path(client_email: str) -> Path:
    safe = client_email.strip().replace("@", "_at_")
    OUTPUT_DIR.mkdir(exist_ok=True)
    return OUTPUT_DIR / f"thread_{safe}.txt"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Экспорт переписки с клиентом в .txt для передачи в LLM."
    )
    parser.add_argument(
        "client_email",
        nargs="?",
        default=None,
        help="Email клиента (иначе CLIENT_EMAIL из .env или значение по умолчанию)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Путь к выходному .txt (по умолчанию exported_emails_txt/thread_<email>.txt)",
    )
    args = parser.parse_args()

    client = (args.client_email or _env("CLIENT_EMAIL") or DEFAULT_CLIENT_EMAIL).strip()
    if not client:
        print("Укажите email клиента аргументом или CLIENT_EMAIL в .env", file=sys.stderr)
        return 1

    print(f"Клиент: {client}")
    emails = _collect_emails(client)
    print(f"Писем: {len(emails)}")
    crm = fetch_crm(client)
    client_question = _extract_client_question_from_crm(crm)
    if not client_question:
        deal_ids, client_ids = _extract_ids_from_crm(crm)
        extra_payloads = _fetch_crm_details_for_question(deal_ids, client_ids)
        for item in extra_payloads:
            client_question = _extract_client_question_from_crm(item)
            if client_question:
                break
    if client_question:
        print("CRM: найдено поле «Вопрос клиента»")
    else:
        print("CRM: поле «Вопрос клиента» не найдено (или не настроены ENVYCRM_*).")

    text = build_thread_text(
        emails,
        client_question=client_question,
        client_email=client,
        manager_email=_env("MAIL_ADRESS"),
    )
    out = args.output if args.output is not None else _default_out_path(client)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"Файл: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

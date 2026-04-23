"""
По email клиента:
  1) Читает данные из EnvyCRM (lead/search + deal/search, только GET-логика POST search).
  2) Загружает из ящика менеджера (Yandex IMAP) переписку с этим адресом (FROM / TO / CC).
  3) Пишет в Google Sheets: лист CRM — только полезные для агента поля; лист Emails — как раньше.

Переменные .env (как в ваших скриптах):
  MAIL_ADRESS, MAIL_KEY — ящик менеджера (Yandex)
  GOOGLE_SHEET_ID, credentials.json, token.json
  ENVYCRM_BASE_URL, ENVYCRM_KEY
  CLIENT_EMAIL — опционально (по умолчанию aluoranen@mail.ru)
"""

from __future__ import annotations

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

import gspread
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993

GOOGLE_CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
OUTPUT_DIR = Path("exported_emails_json")
SHEET_CRM = "CRM_Context"
SHEET_EMAILS = "Emails"

DEFAULT_CLIENT_EMAIL = "aluoranen@mail.ru"


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
    except json.JSONDecodeError:
        parsed = body
    return code, parsed


def fetch_crm(client_email: str) -> dict[str, Any]:
    base = _env("ENVYCRM_BASE_URL").rstrip("/")
    api_key = _env("ENVYCRM_KEY")
    if not base or not api_key:
        raise RuntimeError("Нужны ENVYCRM_BASE_URL и ENVYCRM_KEY в .env")

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


def crm_rows_for_sheet(crm_json: dict[str, Any]) -> list[list[Any]]:
    """
    Только поля, нужные агенту: идентификация, воронка/этап (id), ответственные, даты сделки.
    Одна строка на каждую сделку из deal_search.clients[].deals_for_event.
    """
    headers = [
        "client_email",
        "client_id",
        "client_name",
        "deal_id",
        "pipeline_id",
        "stage_id",
        "status_id",
        "user_id",
        "employee_id",
        "old_employee_id",
        "deal_created_at",
        "stage_updated_at",
        "closed_at",
    ]
    rows: list[list[Any]] = [headers]

    deal_body = (
        crm_json.get("results", {})
        .get("deal_search", {})
        .get("body")
    )
    if not isinstance(deal_body, dict):
        return rows

    clients = deal_body.get("clients") or []
    client_email = crm_json.get("email") or ""

    for cl in clients:
        if not isinstance(cl, dict):
            continue
        cid = cl.get("id")
        cname = cl.get("name")
        for d in cl.get("deals_for_event") or []:
            if not isinstance(d, dict):
                continue
            rows.append(
                [
                    client_email,
                    cid,
                    cname,
                    d.get("id"),
                    d.get("pipeline_id"),
                    d.get("stage_id"),
                    d.get("status_id"),
                    d.get("user_id"),
                    d.get("employee_id"),
                    d.get("old_employee_id"),
                    d.get("created_at"),
                    d.get("stage_updated_at"),
                    d.get("closed_at"),
                ]
            )
    return rows


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def decode_mime_words(s: str | None) -> str:
    if s is None:
        return ""
    decoded_fragments = decode_header(s)
    return "".join(
        str(t, enc or "utf-8") if isinstance(t, bytes) else str(t)
        for t, enc in decoded_fragments
    )


def get_text_from_email(msg: email.message.Message) -> str:
    text_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        text_content = payload.decode(charset, errors="ignore")
                        break
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text_content = payload.decode(charset, errors="ignore")
        except Exception:
            pass
    return text_content.strip()


def _imap_quote_addr(addr: str) -> str:
    return addr.replace("\\", "\\\\").replace('"', '\\"')


def search_email_ids(mail: imaplib.IMAP4_SSL, contact_email: str) -> list[bytes]:
    """Письма, где клиент в FROM, TO или CC (три поиска — надёжнее на Yandex IMAP)."""
    mail.select("INBOX")
    q = _imap_quote_addr(contact_email.strip())
    found: set[bytes] = set()
    for crit in (f'FROM "{q}"', f'TO "{q}"', f'CC "{q}"'):
        status, messages = mail.search(None, crit)
        if status == "OK" and messages and messages[0]:
            found.update(messages[0].split())
    return list(found)


def fetch_emails_by_ids(
    mail: imaplib.IMAP4_SSL, email_ids: list[bytes]
) -> list[dict[str, Any]]:
    seen: set[bytes] = set()
    out: list[dict[str, Any]] = []
    for eid in email_ids:
        if eid in seen:
            continue
        seen.add(eid)
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK" or not msg_data:
            continue
        raw = msg_data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        msg = email.message_from_bytes(raw)
        subject = decode_mime_words(msg["Subject"]) or "Без темы"
        from_header = decode_mime_words(msg["From"]) or ""
        to_header = decode_mime_words(msg["To"]) or ""
        date_str = msg["Date"] or ""
        text_plain = clean_text(get_text_from_email(msg))
        ts = 0.0
        if date_str:
            try:
                ts = parsedate_to_datetime(date_str).timestamp()
            except (TypeError, ValueError, OverflowError):
                ts = 0.0
        out.append(
            {
                "subject": subject,
                "from": from_header,
                "to": to_header,
                "date": date_str,
                "textPlain": text_plain,
                "_ts": ts,
            }
        )
    out.sort(key=lambda x: float(x.get("_ts", 0.0)))
    for item in out:
        item.pop("_ts", None)
    return out


def connect_imap() -> imaplib.IMAP4_SSL:
    addr = _env("MAIL_ADRESS")
    key = _env("MAIL_KEY")
    if not addr or not key:
        raise RuntimeError("Нужны MAIL_ADRESS и MAIL_KEY в .env")
    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(addr, key)
    return m


def get_google_sheets_auth() -> Credentials | None:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, scopes=scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                print("Нет credentials.json — см. инструкцию в старом export_to_sheets.py", file=sys.stderr)
                return None
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, scopes=scopes
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return creds


def open_spreadsheet():
    sid = _env("GOOGLE_SHEET_ID")
    if not sid:
        raise RuntimeError("Нужен GOOGLE_SHEET_ID в .env")
    creds = get_google_sheets_auth()
    if not creds:
        return None
    client = gspread.authorize(creds)
    return client.open_by_key(sid)


def _ensure_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str, rows: int, cols: int
) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def upload_crm_block(spreadsheet: gspread.Spreadsheet, crm_rows: list[list[Any]]) -> None:
    ws = _ensure_worksheet(spreadsheet, SHEET_CRM, max(len(crm_rows) + 10, 50), 14)
    ws.clear()
    if crm_rows:
        ws.update(crm_rows, range_name="A1", raw=False)
    try:
        ws.format(
            "A1:M1",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.9, "green": 0.95, "blue": 0.9},
            },
        )
    except Exception:
        pass


def upload_emails_block(spreadsheet: gspread.Spreadsheet, emails: list[dict[str, Any]]) -> None:
    ws = _ensure_worksheet(
        spreadsheet, SHEET_EMAILS, max(len(emails) + 100, 200), 5
    )
    ws.clear()
    headers = ["Subject", "From", "To", "Date", "Text"]
    all_rows = [headers]
    for e in emails:
        all_rows.append(
            [
                e.get("subject", ""),
                e.get("from", ""),
                e.get("to", ""),
                e.get("date", ""),
                e.get("textPlain", ""),
            ]
        )
    if len(all_rows) > 1:
        ws.update(all_rows, range_name="A1", raw=False)
    try:
        ws.format(
            "A1:E1",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.95},
            },
        )
    except Exception:
        pass


def save_combined_json(
    client_email: str, crm: dict[str, Any], emails: list[dict[str, Any]]
) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"context_{client_email.replace('@', '_at_')}.json"
    payload = {
        "client_email": client_email,
        "crm": crm,
        "emails": emails,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    load_dotenv()
    client_email = _env("CLIENT_EMAIL") or DEFAULT_CLIENT_EMAIL

    print(f"Клиент: {client_email}")

    crm = fetch_crm(client_email)
    crm_rows = crm_rows_for_sheet(crm)
    print(f"CRM: строк для таблицы (с заголовком): {len(crm_rows)}")

    mail = connect_imap()
    try:
        ids = search_email_ids(mail, client_email)
        print(f"IMAP: найдено писем (по FROM/TO/CC): {len(ids)}")
        emails = fetch_emails_by_ids(mail, ids) if ids else []
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()

    path = save_combined_json(client_email, crm, emails)
    print(f"JSON: {path.resolve()}")

    sh = open_spreadsheet()
    if not sh:
        return 1
    upload_crm_block(sh, crm_rows)
    upload_emails_block(sh, emails)
    print(f"Таблица: {sh.url} (листы «{SHEET_CRM}», «{SHEET_EMAILS}»)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

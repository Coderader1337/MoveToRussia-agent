"""
По email клиента:
  1) Читает данные из EnvyCRM (lead/search + deal/search — те же read-only search, что в fetch_envycrm_by_email.py).
  2) Загружает из ящика менеджера (Yandex IMAP) переписку (FROM / TO / CC) без побочных эффектов:
     сначала INBOX, затем папка исходящих (по умолчанию Sent; иначе IMAP_SENT_MAILBOX в .env, напр. Отправленные).
     EXAMINE (readonly), FETCH BODY.PEEK[] (не выставляет \\Seen; Yandex не принимает RFC822.PEEK).
  3) Пишет в Google Sheets: лист CRM — только полезные для агента поля; лист Emails — как раньше.

Переменные .env (как в ваших скриптах):
  MAIL_ADRESS, MAIL_KEY — ящик менеджера (Yandex)
  GOOGLE_SHEET_ID, credentials.json, token.json
  ENVYCRM_BASE_URL, ENVYCRM_KEY
  CLIENT_EMAIL — опционально (по умолчанию aluoranen@mail.ru)
  IMAP_SENT_MAILBOX — опционально, имя папки «Отправленные» в IMAP (если не Sent)
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

DEFAULT_CLIENT_EMAIL = "client@example.com"

# Google Sheets: не более 50 000 символов в одной ячейке (иначе API 400).
# Берём запас — длинные цепочки эмодзи/суррогатов могут по-разному учитываться на стороне API.
GSHEET_MAX_CELL_CHARS = 49_000
_GSHEET_TRUNC_MARKER = " …[обрезано: лимит ячейки Google Sheets]"


def _truncate_for_sheet_cell(value: Any, limit: int = GSHEET_MAX_CELL_CHARS) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    keep = limit - len(_GSHEET_TRUNC_MARKER)
    if keep < 1:
        return _GSHEET_TRUNC_MARKER[:limit]
    return s[:keep] + _GSHEET_TRUNC_MARKER


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
    """
    Только text/plain из MIME-дерева: части text/html не используются (без HTML→текст и без regex).
    Несколько plain-частей (например вложенные alternative) склеиваются через пустую строку.
    """
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
    """Достаёт сырой RFC822 из ответа FETCH (BODY.PEEK[] / RFC822), не полагаясь на одну форму tuple."""
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
    # Первая строка ответа — короткий префикс вида b'1 (BODY[PEEK]...'; тело письма — самый длинный chunk.
    return max(candidates, key=len)


def search_email_ids(
    mail: imaplib.IMAP4_SSL, contact_email: str, mailbox: str = "INBOX"
) -> list[bytes]:
    """Письма в указанной папке, где клиент в FROM, TO или CC."""
    # readonly=True → EXAMINE: только чтение, без записи состояния ящика в этой сессии.
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
        # RFC822 = BODY[] и может выставить \\Seen; BODY.PEEK[] — без флагов (RFC 3501). Yandex: BAD на RFC822.PEEK.
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
        safe = [[_truncate_for_sheet_cell(c) for c in row] for row in crm_rows]
        ws.update(safe, range_name="A1", raw=False)
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
        spreadsheet, SHEET_EMAILS, max(len(emails) + 100, 200), 6
    )
    ws.clear()
    headers = ["Folder", "Subject", "From", "To", "Date", "Text"]
    all_rows = [headers]
    for e in emails:
        all_rows.append(
            [
                _truncate_for_sheet_cell(e.get("folder", "")),
                _truncate_for_sheet_cell(e.get("subject", "")),
                _truncate_for_sheet_cell(e.get("from", "")),
                _truncate_for_sheet_cell(e.get("to", "")),
                _truncate_for_sheet_cell(e.get("date", "")),
                _truncate_for_sheet_cell(e.get("textPlain", "")),
            ]
        )
    if len(all_rows) > 1:
        try:
            ws.update(all_rows, range_name="A1", raw=False)
        except gspread.exceptions.APIError as err:
            # На случай смены лимита или других полей — повтор с более жёсткой обрезкой.
            if "50000" in str(err) or "INVALID_ARGUMENT" in str(err):
                hard = 45_000
                shrunk = [all_rows[0]] + [
                    [_truncate_for_sheet_cell(c, hard) for c in row]
                    for row in all_rows[1:]
                ]
                ws.update(shrunk, range_name="A1", raw=False)
                print(
                    "Предупреждение: повторная загрузка с обрезкой 45k симв. на ячейку после ошибки API.",
                    file=sys.stderr,
                )
            else:
                raise
    try:
        ws.format(
            "A1:F1",
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
        emails: list[dict[str, Any]] = []
        ids_in = search_email_ids(mail, client_email, "INBOX")
        print(f"IMAP INBOX: совпадений поиска: {len(ids_in)}")
        emails.extend(fetch_emails_by_ids(mail, ids_in, "INBOX"))

        sent_mailbox = _env("IMAP_SENT_MAILBOX") or "Sent"
        try:
            ids_out = search_email_ids(mail, client_email, sent_mailbox)
            print(f"IMAP {sent_mailbox}: совпадений поиска: {len(ids_out)}")
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

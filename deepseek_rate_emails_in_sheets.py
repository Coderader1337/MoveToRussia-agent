"""
Оценка писем в Google Sheets через DeepSeek (1–100, только число).

Для каждой строки — до двух изолированных запросов:
  ai_rate      ← колонка deepseek_response
  manager_rate ← колонка first_email_from_manager

Вход: критерии + первое сообщение клиента + одно письмо. Без примеров и без контекста других писем.

.env: DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_TEMPERATURE
      DEEPSEEK_SHEET_ID (или по умолчанию), credentials.json, token.json

Примеры:
  python deepseek_rate_emails_in_sheets.py
  python deepseek_rate_emails_in_sheets.py --limit 5
  python deepseek_rate_emails_in_sheets.py --email cluke92@icloud.com
  python deepseek_rate_emails_in_sheets.py --only manager --force
  python deepseek_rate_emails_in_sheets.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import gspread
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent
CRITERIA_FILE = ROOT / "prompts" / "deepseek_email_rating_criteria.txt"
RATING_LOG = ROOT / "deepseek_rating.log"

DEFAULT_SHEET_ID = "11j3PD1d1Jzmq_L3XjUh60tflBgT7QWfva9EhPuf8AcM"
DEFAULT_WORKSHEET_GID = 1004740013

GOOGLE_CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_TEMPERATURE = 0.3

COL_EMAIL = 0
COL_AI_LETTER = 1
COL_FIRST_MSG = 2
COL_MGR_LETTER = 3
COL_AI_RATE = 4
COL_MGR_RATE = 5

SYSTEM_PROMPT = (
    "Ты — объективный оценщик деловых email на английском. "
    "Оцениваешь только одно переданное письмо, изолированно. "
    "Не сравнивай с другими письмами, не используй внешние примеры. "
    "Ответ: строго одно целое число от 1 до 100, без пробелов, без текста, без пояснений, без JSON."
)

_QUOTE_CUT = re.compile(
    r"\n\s*-{8,}\s*\n|^\s*On .+wrote:\s*$|Original Message",
    re.I | re.M,
)


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with RATING_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_criteria() -> str:
    if not CRITERIA_FILE.is_file():
        raise FileNotFoundError(f"Нет файла критериев: {CRITERIA_FILE}")
    return CRITERIA_FILE.read_text(encoding="utf-8").strip()


def letter_body_for_rating(raw: str) -> str:
    """Только текст письма для оценки, без служебной шапки и без цитат переписки."""
    text = raw.strip()
    if not text:
        return ""
    parts = text.split("-" * 40, 1)
    if len(parts) > 1:
        text = parts[1].strip()
    m = _QUOTE_CUT.search(text)
    if m:
        text = text[: m.start()].strip()
    return text


def has_nested_thread(text: str) -> bool:
    body = letter_body_for_rating(text)
    if not body:
        return False
    if re.search(r"-{8,}", body):
        return True
    if len(re.findall(r"(?im)^Dear (?:Mr\.|Ms\.|Mrs\.)", body)) > 1:
        return True
    if re.search(r"escribió:", body, re.I):
        return True
    return False


def build_rating_prompt(
    criteria: str, client_question: str, letter: str
) -> str:
    return f"""
=== КРИТЕРИИ ОЦЕНКИ ===
{criteria}

=== ПЕРВОЕ ОБРАЩЕНИЕ КЛИЕНТА ===
{client_question.strip()}

=== ПИСЬМО ДЛЯ ОЦЕНКИ ===
{letter.strip()}

=== ЗАДАЧА ===
По критериям выше оцени только блок «ПИСЬМО ДЛЯ ОЦЕНКИ» в контексте первого обращения клиента.
Верни строго одно целое число от 1 до 100. Никакого другого текста.
""".strip()


def call_deepseek_score(user_prompt: str) -> int:
    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Нужен DEEPSEEK_API_KEY в .env")

    model = _env("DEEPSEEK_MODEL") or DEEPSEEK_MODEL
    temp_raw = _env("DEEPSEEK_TEMPERATURE")
    temperature = float(temp_raw) if temp_raw else DEEPSEEK_TEMPERATURE

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DeepSeek HTTP {e.code}: {e.read().decode()}") from e

    raw = str(body["choices"][0]["message"]["content"]).strip()
    return parse_score(raw)


def parse_score(raw: str) -> int:
    raw = raw.strip().strip("`").strip()
    if re.fullmatch(r"\d{1,3}", raw):
        n = int(raw)
        if 1 <= n <= 100:
            return n
    m = re.search(r"\b(\d{1,3})\b", raw)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 100:
            return n
    raise ValueError(f"Не удалось разобрать оценку 1–100: {raw!r}")


def get_google_sheets_auth() -> Credentials:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = None
    token_path = _env("GOOGLE_TOKEN_FILE") or TOKEN_FILE
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes=scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds_file = _env("GOOGLE_CREDENTIALS_FILE") or GOOGLE_CREDENTIALS_FILE
            if not os.path.exists(creds_file):
                raise FileNotFoundError(f"Нет {creds_file}")
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, scopes=scopes)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def open_worksheet(sheet_id: str, worksheet_gid: int) -> gspread.Worksheet:
    client = gspread.authorize(get_google_sheets_auth())
    spreadsheet = client.open_by_key(sheet_id)
    for ws in spreadsheet.worksheets():
        if ws.id == worksheet_gid:
            return ws
    raise RuntimeError(f"Лист gid={worksheet_gid} не найден")


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


def update_rate(ws: gspread.Worksheet, row_num: int, col_idx: int, score: int) -> None:
    col_letter = chr(ord("A") + col_idx)
    ws.update(values=[[str(score)]], range_name=f"{col_letter}{row_num}", value_input_option="RAW")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Оценка писем в Sheets через DeepSeek (1–100)")
    p.add_argument("--email", help="Только этот client_email")
    p.add_argument("--limit", type=int, default=0, help="Макс. строк для обработки (0 = все)")
    p.add_argument(
        "--only",
        choices=("ai", "manager", "both"),
        default="both",
        help="Что оценивать: ai, manager или both",
    )
    p.add_argument("--force", action="store_true", help="Перезаписать уже заполненные оценки")
    p.add_argument("--pause", type=float, default=0.5, help="Пауза между запросами, сек")
    p.add_argument("--dry-run", action="store_true", help="Без вызова DeepSeek и без записи")
    p.add_argument("--sheet-id", default="")
    p.add_argument("--worksheet-gid", type=int, default=DEFAULT_WORKSHEET_GID)
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    sheet_id = args.sheet_id or _env("DEEPSEEK_SHEET_ID") or DEFAULT_SHEET_ID
    criteria = load_criteria()

    ws = open_worksheet(sheet_id, args.worksheet_gid)
    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        print("В таблице нет данных", file=sys.stderr)
        return 1

    header = all_rows[0]
    _log(f"Заголовок: {header}")

    processed = 0
    rated_ai = 0
    rated_mgr = 0
    skipped = 0
    errors = 0

    for row_num, row in enumerate(all_rows[1:], start=2):
        if args.limit and processed >= args.limit:
            break

        email = _cell(row, COL_EMAIL)
        if not email:
            continue
        if args.email and email.lower() != args.email.strip().lower():
            continue

        client_q = _cell(row, COL_FIRST_MSG)
        ai_letter_raw = _cell(row, COL_AI_LETTER)
        mgr_letter_raw = _cell(row, COL_MGR_LETTER)
        ai_rate_existing = _cell(row, COL_AI_RATE)
        mgr_rate_existing = _cell(row, COL_MGR_RATE)

        if not client_q:
            _log(f"SKIP row {row_num} {email}: нет first_message")
            skipped += 1
            continue

        row_touched = False

        tasks: list[tuple[str, str, int]] = []
        if args.only in ("ai", "both") and ai_letter_raw:
            if args.force or not ai_rate_existing:
                tasks.append(("ai", ai_letter_raw, COL_AI_RATE))
        if args.only in ("manager", "both") and mgr_letter_raw:
            if args.force or not mgr_rate_existing:
                tasks.append(("manager", mgr_letter_raw, COL_MGR_RATE))

        if not tasks:
            skipped += 1
            continue

        for kind, letter_raw, col_idx in tasks:
            if kind == "manager" and has_nested_thread(letter_raw):
                _log(f"SKIP row {row_num} {email}: manager_rate — вложенная переписка")
                skipped += 1
                continue

            letter = letter_body_for_rating(letter_raw)
            if len(letter) < 50:
                _log(f"SKIP row {row_num} {email}: {kind} — слишком короткое письмо")
                skipped += 1
                continue

            prompt = build_rating_prompt(criteria, client_q, letter)

            if args.dry_run:
                _log(f"DRY row {row_num} {email} {kind}: prompt {len(prompt)} sym")
                row_touched = True
                continue

            try:
                score = call_deepseek_score(prompt)
                update_rate(ws, row_num, col_idx, score)
                _log(f"OK row {row_num} {email} {kind}_rate={score}")
                if kind == "ai":
                    rated_ai += 1
                else:
                    rated_mgr += 1
                row_touched = True
            except Exception as exc:
                errors += 1
                _log(f"ERR row {row_num} {email} {kind}: {exc}")

            if args.pause > 0:
                time.sleep(args.pause)

        if row_touched:
            processed += 1

    _log(
        f"Итог: строк затронуто={processed}, ai={rated_ai}, manager={rated_mgr}, "
        f"пропущено={skipped}, ошибок={errors}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

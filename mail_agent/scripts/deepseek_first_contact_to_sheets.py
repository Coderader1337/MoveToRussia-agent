"""
Первый контакт: CRM + IMAP + DeepSeek → Google Sheets.

Колонки: client_email | deepseek_response | first_message | first_email_from_manager

Только клиенты менеджера CRM_MANAGER_EMPLOYEE_ID (по умолчанию 1137861).
Первое письмо менеджера — самое раннее из ящиков e.novik и a.antonova (Yandex IMAP Sent).

CRM — только чтение. IMAP — readonly (EXAMINE, BODY.PEEK[]).

.env:
  DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_TEMPERATURE
  ENVYCRM_BASE_URL, ENVYCRM_KEY, CRM_MANAGER_EMPLOYEE_ID
  E_NOVIK_MAIL_ADRESS, E_NOVIK_MAIL_KEY
  A_ANTONOVA_MAIL_ADRESS, A_ANTONOVA_MAIL_KEY
  IMAP_SENT_MAILBOX (опционально)
  DEEPSEEK_SHEET_ID, credentials.json, token.json

Примеры:
  python deepseek_first_contact_to_sheets.py
  python deepseek_first_contact_to_sheets.py --email client@example.com
  python deepseek_first_contact_to_sheets.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import gspread
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from export_client_thread_to_txt import (
    _extract_client_question_from_crm,
    _extract_ids_from_crm,
    _fetch_crm_details_for_question,
    _post_crm,
    fetch_crm,
)
from mail_imap_utils import fetch_first_manager_email_to_client

ROOT = Path(__file__).resolve().parent
EXAMPLES_DIR = ROOT / "exported_emails_txt"
PRINCIPLES_FILE = ROOT / "prompts" / "deepseek_first_contact_principles.txt"
CRM_EMAILS_CSV = ROOT / "crm_client_emails.csv"
PENDING_CSV = ROOT / "deepseek_sheet_pending.csv"
BATCH_LOG = ROOT / "deepseek_batch.log"

DEFAULT_SHEET_ID = "11j3PD1d1Jzmq_L3XjUh60tflBgT7QWfva9EhPuf8AcM"
DEFAULT_WORKSHEET_GID = 1004740013

GOOGLE_CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_TEMPERATURE = 0.3
DEFAULT_MANAGER_EMPLOYEE_ID = 1137861
GSHEET_MAX_CELL_CHARS = 49_000
_SHEET_COLUMNS = (
    "client_email",
    "deepseek_response",
    "first_message",
    "first_email_from_manager",
)

# Только read-only эндпоинты CRM.
ALLOWED_CRM_PATHS = frozenset(
    {
        "/crm/api/v1/lead/search/",
        "/crm/api/v1/deal/search/",
        "/crm/api/v1/deal/get/",
        "/crm/api/v1/client/get/",
        "/crm/api/v1/client/search/",
    }
)

SYSTEM_PROMPT = """Ты — менеджер MovetoRussia.com. Пишешь клиенту на английском, тон — живой человек, не бот: вежливо, спокойно, по делу. Главная бизнес-цель: следовать этапам воронки. Страны, из которых возможен переезд по shared values visa: страны ЕС, США, Канада, Австралия, все недружественные для России государства. Этапы воронки: 1. Запрос 2. Первый контакт 3. Получили ответ 4. Предложили созвон. 5. Клиент подтвердил созвон. 6. Созвон состоялся. 7. Предложение отправлено. 8. Обратная связь получена. 9. Согласование условий. 10. Клиенту отослали контракт и счет, предложили время для звонка. Ты должен сначала определить, на каком этапе воронки мы находимся и только потом придумывать целесообразный ответ. Правила: ответь на вопросы клиента из переписки; не выдумывай факты, ссылки, сроки и гарантии; не обещай 100% результат, но и не отклоняй сильно запросы клиента; не упоминай ИИ/автоматизацию; чувствительные темы — мягко и обтекаемо, без отпугивания. Обнадеживай, ищи обходные пути сомнительных тем. Предложи текст следующего письма клиенту. Напиши только письмо, больше никаких дополнительных пояснений."""


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def _assert_readonly_crm_path(path: str) -> None:
    normalized = "/" + path.strip("/") + "/"
    if normalized not in ALLOWED_CRM_PATHS:
        raise RuntimeError(
            f"Запрещённый путь CRM API: {path!r}. Разрешены только: "
            + ", ".join(sorted(ALLOWED_CRM_PATHS))
        )


def _post_crm_readonly(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    _assert_readonly_crm_path(path)
    return _post_crm(path, payload)


def _extract_manager_letter_from_example(path: Path) -> str:
    """Ответ менеджера из 1.txt / 2.txt / 3.txt (блок с Dear Mr./Ms.)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(
        r"(Dear (?:Mr\.|Ms\.|Mrs\.)[^\n]+,[\s\S]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Не найден ответ менеджера в {path}")
    letter = m.group(1).strip()
    # Обрезаем подпись после movetorussia.com / email менеджера
    cut = re.search(
        r"\n\s*(?:Kind regards|Best regards|Sincerely),?\s*\n",
        letter,
        flags=re.IGNORECASE,
    )
    if cut:
        letter = letter[: cut.start()].strip()
    return letter


def load_example_templates() -> str:
    parts: list[str] = []
    for i, fname in enumerate(("1.txt", "2.txt", "3.txt"), start=1):
        path = EXAMPLES_DIR / fname
        if not path.is_file():
            raise FileNotFoundError(f"Нет файла примера: {path}")
        letter = _extract_manager_letter_from_example(path)
        parts.append(f"=== ПРИМЕР {i} ===\n{letter}")
    return "\n\n".join(parts)


def load_communication_principles() -> str:
    if PRINCIPLES_FILE.is_file():
        return PRINCIPLES_FILE.read_text(encoding="utf-8").strip()
    return ""


def _manager_employee_id() -> int:
    raw = _env("CRM_MANAGER_EMPLOYEE_ID")
    if raw:
        return int(raw)
    return DEFAULT_MANAGER_EMPLOYEE_ID


def _truncate_cell(value: str, limit: int = GSHEET_MAX_CELL_CHARS) -> str:
    if len(value) <= limit:
        return value
    marker = " …[обрезано]"
    return value[: limit - len(marker)] + marker


def client_belongs_to_manager(crm: dict[str, Any], employee_id: int) -> bool:
    """Сделка клиента закреплена за менеджером employee_id (deal_search)."""
    clients = (
        crm.get("results", {})
        .get("deal_search", {})
        .get("body", {})
        .get("clients", [])
    )
    if not isinstance(clients, list):
        return False
    for cl in clients:
        if not isinstance(cl, dict):
            continue
        for d in cl.get("deals_for_event") or []:
            if isinstance(d, dict) and d.get("employee_id") == employee_id:
                return True
    return False


def _client_name_from_crm(crm: dict[str, Any]) -> str:
    clients = (
        crm.get("results", {})
        .get("deal_search", {})
        .get("body", {})
        .get("clients", [])
    )
    if isinstance(clients, list) and clients:
        cl = clients[0]
        if isinstance(cl, dict):
            return str(cl.get("name") or "").strip()
    return ""


def _nationality_from_crm_payload(node: Any) -> str:
    markers = ("nationality", "национальность", "country of residence", "страна")
    found: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            nm = str(n.get("name", "")).strip().lower()
            if nm and any(m in nm for m in markers):
                val = str(n.get("value", "")).strip()
                if val:
                    found.append(val)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return found[0] if found else ""


def fetch_first_message_and_meta(
    client_email: str, *, employee_id: int | None = None
) -> dict[str, str]:
    crm = fetch_crm(client_email)
    mgr_id = employee_id if employee_id is not None else _manager_employee_id()
    if not client_belongs_to_manager(crm, mgr_id):
        return {
            "client_email": client_email,
            "client_name": "",
            "client_nationality": "",
            "first_message": "",
            "first_email_from_manager": "",
            "deal_ids": "",
            "manager_employee_id": str(mgr_id),
            "assigned_to_manager": "no",
        }
    deal_ids, client_ids = _extract_ids_from_crm(crm)
    extras = _fetch_crm_details_for_question(deal_ids, client_ids)

    question = _extract_client_question_from_crm(crm)
    nationality = ""

    for blob in extras:
        q2 = _extract_client_question_from_crm(blob)
        if len(q2) > len(question):
            question = q2
        nat = _nationality_from_crm_payload(blob.get("body"))
        if nat and not nationality:
            nationality = nat

    if not nationality:
        nationality = _nationality_from_crm_payload(crm)

    name = _client_name_from_crm(crm)
    if not name and client_ids:
        code, body = _post_crm_readonly(
            "/crm/api/v1/client/get/", {"request": {"id": client_ids[0]}}
        )
        if code == 200 and isinstance(body, dict):
            result = body.get("result") or body.get("client") or body
            if isinstance(result, dict):
                name = str(result.get("name") or name).strip()
            nationality = nationality or _nationality_from_crm_payload(body)

    first_mgr_email = ""
    if question.strip():
        try:
            first_mgr_email = fetch_first_manager_email_to_client(client_email)
        except Exception as exc:
            print(f"IMAP: не удалось получить письмо менеджера: {exc}", file=sys.stderr)

    return {
        "client_email": client_email,
        "client_name": name,
        "client_nationality": nationality,
        "first_message": question.strip(),
        "first_email_from_manager": first_mgr_email,
        "deal_ids": ",".join(str(x) for x in deal_ids),
        "manager_employee_id": str(mgr_id),
        "assigned_to_manager": "yes",
    }


def build_user_prompt(
    *,
    client_email: str,
    client_name: str,
    client_nationality: str,
    first_message: str,
    example_templates: str,
    principles: str,
) -> str:
    return f"""
{principles}

=== ИНФОРМАЦИЯ О КЛИЕНТЕ ===
Email: {client_email}
Имя: {client_name or "—"}
Национальность: {client_nationality or "—"}

=== ПЕРВОЕ ОБРАЩЕНИЕ КЛИЕНТА (с сайта) ===
{first_message}

=== ИСТОРИЯ ПЕРЕПИСКИ ===
(пусто — это первый контакт, ответь только на первое обращение)

=== ПРИМЕРЫ ИДЕАЛЬНЫХ ОТВЕТОВ ===
{example_templates}

=== ЗАДАЧА ===
Клиент на этапе «Первый контакт». Напиши первое письмо-ответ менеджера на английском для {client_email}.
Ответь на все вопросы из первого обращения. Будь вежлив, предсказуем и заботлив.
Не предлагай созвон прямо сейчас — только ответ на вопросы и удержание клиента.
Напиши только текст письма (без темы и без пояснений на русском).
""".strip()


def call_deepseek(system_prompt: str, user_prompt: str) -> str:
    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Нужен DEEPSEEK_API_KEY в .env")

    model = _env("DEEPSEEK_MODEL") or DEEPSEEK_MODEL
    temp_raw = _env("DEEPSEEK_TEMPERATURE")
    temperature = float(temp_raw) if temp_raw else DEEPSEEK_TEMPERATURE

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
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
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {e.code}: {err}") from e

    try:
        return str(body["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Неожиданный ответ DeepSeek: {body!r}") from e


def _credentials_path() -> str:
    return _env("GOOGLE_CREDENTIALS_FILE") or GOOGLE_CREDENTIALS_FILE


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
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            creds_file = _credentials_path()
            if not os.path.exists(creds_file):
                raise FileNotFoundError(
                    f"Нет {creds_file} — OAuth для Google Sheets "
                    "(как в export_client_context_to_sheets.py)"
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, scopes=scopes)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return creds


def open_output_worksheet(
    sheet_id: str, worksheet_gid: int
) -> gspread.Worksheet:
    client = gspread.authorize(get_google_sheets_auth())
    spreadsheet = client.open_by_key(sheet_id)
    for ws in spreadsheet.worksheets():
        if ws.id == worksheet_gid:
            return ws
    titles = [w.title for w in spreadsheet.worksheets()]
    raise RuntimeError(
        f"Лист с gid={worksheet_gid} не найден. Доступные: {titles}"
    )


def save_pending_row(
    client_email: str,
    deepseek_response: str,
    first_message: str,
    first_email_from_manager: str = "",
    path: Path = PENDING_CSV,
) -> None:
    """Резервная запись, если Sheets недоступен."""
    new_file = not path.is_file()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(list(_SHEET_COLUMNS))
        writer.writerow(
            [client_email, deepseek_response, first_message, first_email_from_manager]
        )


def append_result_row(
    ws: gspread.Worksheet,
    client_email: str,
    deepseek_response: str,
    first_message: str,
    first_email_from_manager: str,
) -> int:
    """A:D — email, deepseek, first_message, first_email_from_manager."""
    col_a = ws.col_values(1)
    row_num = max(len(col_a) + 1, 2)
    row = [
        client_email,
        _truncate_cell(deepseek_response),
        _truncate_cell(first_message),
        _truncate_cell(first_email_from_manager),
    ]
    ws.update(
        values=[row],
        range_name=f"A{row_num}:D{row_num}",
        value_input_option="RAW",
    )
    return row_num


def pick_random_email(csv_path: Path) -> str:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("email") or "").strip()]
    if not rows:
        raise RuntimeError(f"В {csv_path} нет email")
    return random.choice(rows)["email"].strip()


def _log_batch(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with BATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_emails_from_csv(csv_path: Path) -> list[str]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return [r["email"].strip() for r in csv.DictReader(f) if r.get("email", "").strip()]


def get_sheet_processed_emails(ws: gspread.Worksheet) -> set[str]:
    values = ws.col_values(1)
    if len(values) <= 1:
        return set()
    return {v.strip().lower() for v in values[1:] if v and v.strip()}


def meta_is_complete(meta: dict[str, str], require_manager_email: bool) -> bool:
    if meta.get("assigned_to_manager") != "yes":
        return False
    if not (meta.get("first_message") or "").strip():
        return False
    if require_manager_email and not (meta.get("first_email_from_manager") or "").strip():
        return False
    return True


def process_one_client(
    meta: dict[str, str],
    *,
    examples: str,
    principles: str,
    dry_run: bool = False,
) -> str | None:
    """DeepSeek для одного клиента. Возвращает ответ или None при dry-run."""
    user_prompt = build_user_prompt(
        client_email=meta["client_email"],
        client_name=meta["client_name"],
        client_nationality=meta["client_nationality"],
        first_message=meta["first_message"],
        example_templates=examples,
        principles=principles,
    )
    if dry_run:
        return None
    return call_deepseek(SYSTEM_PROMPT, user_prompt)


def run_batch(
    *,
    limit: int,
    sheet_id: str,
    worksheet_gid: int,
    csv_path: Path,
    employee_id: int,
    pause_sec: float,
    dry_run: bool,
    no_sheets: bool,
    require_manager_email: bool,
) -> int:
    if not csv_path.is_file():
        print(f"Нет {csv_path}", file=sys.stderr)
        return 1

    examples = load_example_templates()
    principles = load_communication_principles()
    pool = load_emails_from_csv(csv_path)
    random.shuffle(pool)

    ws: gspread.Worksheet | None = None
    done_emails: set[str] = set()
    if not dry_run and not no_sheets:
        ws = open_output_worksheet(sheet_id, worksheet_gid)
        done_emails = get_sheet_processed_emails(ws)
        _log_batch(f"Уже в таблице: {len(done_emails)} email")

    success = 0
    skipped = 0
    errors = 0
    scanned = 0

    _log_batch(
        f"Старт batch: цель={limit}, менеджер={employee_id}, "
        f"требуется письмо менеджера={require_manager_email}"
    )

    for email in pool:
        if success >= limit:
            break
        scanned += 1
        key = email.lower()
        if key in done_emails:
            skipped += 1
            continue

        try:
            meta = fetch_first_message_and_meta(email, employee_id=employee_id)
        except Exception as exc:
            errors += 1
            _log_batch(f"ERR CRM/IMAP {email}: {exc}")
            continue

        if not meta_is_complete(meta, require_manager_email):
            skipped += 1
            continue

        _log_batch(
            f"[{success + 1}/{limit}] {email} | msg={len(meta['first_message'])} "
            f"| mgr_mail={len(meta.get('first_email_from_manager') or '')}"
        )

        if dry_run:
            process_one_client(meta, examples=examples, principles=principles, dry_run=True)
            success += 1
            done_emails.add(key)
            continue

        try:
            response = process_one_client(
                meta, examples=examples, principles=principles, dry_run=False
            )
        except Exception as exc:
            errors += 1
            _log_batch(f"ERR DeepSeek {email}: {exc}")
            continue

        if no_sheets:
            save_pending_row(
                meta["client_email"],
                response,
                meta["first_message"],
                meta.get("first_email_from_manager", ""),
            )
        elif ws is not None:
            try:
                row = append_result_row(
                    ws,
                    meta["client_email"],
                    response,
                    meta["first_message"],
                    meta.get("first_email_from_manager", ""),
                )
                _log_batch(f"OK Sheets row {row}: {email}")
            except Exception as exc:
                errors += 1
                save_pending_row(
                    meta["client_email"],
                    response,
                    meta["first_message"],
                    meta.get("first_email_from_manager", ""),
                )
                _log_batch(f"ERR Sheets {email}, в pending: {exc}")
                continue

        success += 1
        done_emails.add(key)

        if pause_sec > 0 and success < limit:
            time.sleep(pause_sec)

    _log_batch(
        f"Готово: успешно={success}/{limit}, пропущено={skipped}, "
        f"ошибок={errors}, просмотрено={scanned}"
    )
    return 0 if success >= limit else (1 if success == 0 else 0)


def pick_random_email_with_message(
    csv_path: Path,
    employee_id: int | None = None,
    max_tries: int = 25,
) -> tuple[str, dict[str, str]]:
    mgr_id = employee_id if employee_id is not None else _manager_employee_id()
    tried: set[str] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        pool = [r["email"].strip() for r in csv.DictReader(f) if r.get("email", "").strip()]
    random.shuffle(pool)
    for email in pool[:max_tries]:
        if email in tried:
            continue
        tried.add(email)
        meta = fetch_first_message_and_meta(email, employee_id=mgr_id)
        if meta_is_complete(meta, require_manager_email=True):
            return email, meta
    raise RuntimeError(
        f"Не найден клиент менеджера {mgr_id} с первым сообщением в CRM "
        f"за {max_tries} попыток"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DeepSeek первый контакт → Google Sheets")
    p.add_argument("--email", help="Email клиента (иначе случайный из crm_client_emails.csv)")
    p.add_argument(
        "--sheet-id",
        default="",
        help=f"ID Google таблицы (по умолчанию DEEPSEEK_SHEET_ID или {DEFAULT_SHEET_ID})",
    )
    p.add_argument(
        "--worksheet-gid",
        type=int,
        default=DEFAULT_WORKSHEET_GID,
        help="gid листа в таблице",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Собрать промпт и показать first_message, без DeepSeek и Sheets",
    )
    p.add_argument("--no-sheets", action="store_true", help="Только DeepSeek, без записи в таблицу")
    p.add_argument(
        "--batch",
        type=int,
        metavar="N",
        help="Обработать N клиентов подряд (менеджер + первое сообщение + письмо менеджера)",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="Пауза между клиентами в batch, сек (по умолчанию 1)",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    sheet_id = (
        args.sheet_id
        or _env("DEEPSEEK_SHEET_ID")
        or DEFAULT_SHEET_ID
    )

    mgr_id = _manager_employee_id()
    print(f"Фильтр CRM: employee_id={mgr_id}")

    if args.batch:
        return run_batch(
            limit=args.batch,
            sheet_id=sheet_id,
            worksheet_gid=args.worksheet_gid,
            csv_path=CRM_EMAILS_CSV,
            employee_id=mgr_id,
            pause_sec=args.pause,
            dry_run=args.dry_run,
            no_sheets=args.no_sheets,
            require_manager_email=True,
        )

    examples = load_example_templates()
    principles = load_communication_principles()

    if args.email:
        email = args.email.strip()
        meta = fetch_first_message_and_meta(email, employee_id=mgr_id)
        if meta.get("assigned_to_manager") != "yes":
            print(
                f"Клиент {email} не закреплён за менеджером {mgr_id}",
                file=sys.stderr,
            )
            return 1
        if not meta_is_complete(meta, require_manager_email=True):
            print(
                f"Клиент {email}: нет первого сообщения CRM и/или письма менеджера",
                file=sys.stderr,
            )
            return 1
    else:
        if not CRM_EMAILS_CSV.is_file():
            print(f"Нет {CRM_EMAILS_CSV}", file=sys.stderr)
            return 1
        email, meta = pick_random_email_with_message(CRM_EMAILS_CSV, employee_id=mgr_id)
        print(f"Случайный клиент (менеджер {mgr_id}): {email}")

    user_prompt = build_user_prompt(
        client_email=meta["client_email"],
        client_name=meta["client_name"],
        client_nationality=meta["client_nationality"],
        first_message=meta["first_message"],
        example_templates=examples,
        principles=principles,
    )

    print(f"Имя: {meta['client_name'] or '—'}")
    print(f"Первое сообщение ({len(meta['first_message'])} симв.): {meta['first_message'][:120]}...")
    mgr_mail = meta.get("first_email_from_manager") or ""
    print(
        f"Первое письмо менеджера ({len(mgr_mail)} симв.): "
        f"{mgr_mail[:100] + '...' if len(mgr_mail) > 100 else mgr_mail or '—'}"
    )

    if args.dry_run:
        print(f"\n--- user prompt ({len(user_prompt)} симв.) ---\n")
        print(user_prompt[:2000])
        if len(user_prompt) > 2000:
            print("\n...[обрезано]...")
        return 0

    print("Запрос к DeepSeek...")
    response = call_deepseek(SYSTEM_PROMPT, user_prompt)
    print(f"Ответ DeepSeek ({len(response)} симв.):\n{response[:400]}...")

    if args.no_sheets:
        save_pending_row(
            meta["client_email"],
            response,
            meta["first_message"],
            meta.get("first_email_from_manager", ""),
        )
        print(f"Без Sheets: сохранено в {PENDING_CSV.resolve()}")
        return 0

    try:
        ws = open_output_worksheet(sheet_id, args.worksheet_gid)
        row = append_result_row(
            ws,
            meta["client_email"],
            response,
            meta["first_message"],
            meta.get("first_email_from_manager", ""),
        )
        print(
            f"Записано в строку {row}: "
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={args.worksheet_gid}"
        )
    except FileNotFoundError as e:
        save_pending_row(
            meta["client_email"],
            response,
            meta["first_message"],
            meta.get("first_email_from_manager", ""),
        )
        print(f"Google Sheets: {e}", file=sys.stderr)
        print(
            f"Ответ DeepSeek сохранён локально: {PENDING_CSV.resolve()}\n"
            "Положите credentials.json в корень проекта и запустите снова "
            "(или импортируйте CSV в таблицу вручную).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

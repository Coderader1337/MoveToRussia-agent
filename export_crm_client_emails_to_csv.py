"""
Выгрузка почтовых ящиков всех клиентов из EnvyCRM в CSV.

Только чтение: используется единственный эндпоинт POST /crm/api/v1/client/list/
(список клиентов). Никаких create/update/delete/set и т.п.

Переменные .env:
  ENVYCRM_BASE_URL, ENVYCRM_KEY

Примеры:
  python export_crm_client_emails_to_csv.py
  python export_crm_client_emails_to_csv.py -o clients_emails.csv
  python export_crm_client_emails_to_csv.py --dry-run
  python export_crm_client_emails_to_csv.py --page-size 100
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Единственный разрешённый путь API (read-only list).
ALLOWED_CRM_PATH = "/crm/api/v1/client/list/"

# type_id контакта «email» в EnvyCRM (см. contacts[].type_id в ответе list).
CONTACT_TYPE_EMAIL = 4

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_OUTPUT = Path("crm_client_emails.csv")
DEFAULT_PAGE_SIZE = 100
REQUEST_TIMEOUT_SEC = 60
REQUEST_PAUSE_SEC = 0.15


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def _assert_readonly_path(path: str) -> None:
    normalized = "/" + path.strip("/") + "/"
    if normalized != ALLOWED_CRM_PATH:
        raise RuntimeError(
            f"Запрещённый путь CRM API: {path!r}. "
            f"Скрипт работает только с {ALLOWED_CRM_PATH}"
        )


def _post_json_readonly(url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    parsed = urllib.parse.urlparse(url)
    _assert_readonly_path(parsed.path)

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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        code = e.code
    try:
        parsed_body: Any = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        parsed_body = body
    return code, parsed_body


def _list_url() -> str:
    base = _env("ENVYCRM_BASE_URL").rstrip("/")
    api_key = _env("ENVYCRM_KEY")
    if not base or not api_key:
        raise RuntimeError("Нужны ENVYCRM_BASE_URL и ENVYCRM_KEY в .env")
    q = urllib.parse.urlencode({"api_key": api_key})
    return f"{base}{ALLOWED_CRM_PATH}?{q}"


def fetch_client_page(offset: int, limit: int) -> dict[str, Any]:
    url = _list_url()
    body = {"request": {"keyword": "", "limit": limit, "offset": offset}}
    code, parsed = _post_json_readonly(url, body)
    if code != 200:
        raise RuntimeError(f"CRM client/list HTTP {code}: {parsed!r}")
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Неожиданный ответ CRM: {parsed!r}")
    if parsed.get("status_code") not in (200, "200", None):
        raise RuntimeError(
            f"CRM вернул status_code={parsed.get('status_code')}: "
            f"{parsed.get('message')!r}"
        )
    return parsed


def _normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    email = value.strip().lower()
    if not email or "@" not in email:
        return ""
    return email if EMAIL_RE.match(email) else ""


def _emails_from_client(client: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        e = _normalize_email(raw)
        if e and e not in seen:
            seen.add(e)
            found.append(e)

    add(client.get("email"))

    contacts = client.get("contacts")
    if isinstance(contacts, list):
        for c in contacts:
            if not isinstance(c, dict):
                continue
            if c.get("type_id") == CONTACT_TYPE_EMAIL:
                add(c.get("value"))
            else:
                # На случай нестандартного type_id — только явные email-строки.
                add(c.get("value"))

    return found


def build_rows(clients: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for cl in clients:
        emails = _emails_from_client(cl)
        primary = emails[0] if emails else ""
        extra = "; ".join(emails[1:]) if len(emails) > 1 else ""
        rows.append(
            {
                "client_id": str(cl.get("id", "")),
                "name": str(cl.get("name") or "").strip(),
                "email": primary,
                "additional_emails": extra,
                "phone": str(cl.get("phone") or "").strip(),
                "created_at": str(cl.get("created_at") or "").strip(),
                "updated_at": str(cl.get("updated_at") or "").strip(),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "client_id",
        "name",
        "email",
        "additional_emails",
        "phone",
        "created_at",
        "updated_at",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Выгрузка email клиентов из EnvyCRM в CSV (только чтение)."
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Путь к CSV (по умолчанию {DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        metavar="N",
        help=f"Размер страницы client/list (по умолчанию {DEFAULT_PAGE_SIZE})",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=REQUEST_PAUSE_SEC,
        help="Пауза между запросами к API, сек (0 — без паузы)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Только запросить первую страницу и показать count, без записи CSV",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    if args.page_size < 1 or args.page_size > 500:
        print("page-size должен быть от 1 до 500", file=sys.stderr)
        return 2

    print(f"CRM (read-only): {_env('ENVYCRM_BASE_URL')}{ALLOWED_CRM_PATH}")

    if args.dry_run:
        page = fetch_client_page(0, min(args.page_size, 10))
        count = page.get("count", "?")
        sample = page.get("result") or []
        print(f"dry-run: всего клиентов в CRM (count): {count}")
        print(f"dry-run: в первой странице записей: {len(sample)}")
        if sample and isinstance(sample[0], dict):
            emails = _emails_from_client(sample[0])
            print(f"dry-run: пример client_id={sample[0].get('id')} email={emails}")
        return 0

    clients: list[dict[str, Any]] = []
    offset = 0
    total_reported: int | None = None
    pages = 0

    while True:
        page = fetch_client_page(offset, args.page_size)
        pages += 1

        if total_reported is None and isinstance(page.get("count"), int):
            total_reported = page["count"]
            print(f"Всего клиентов в CRM (count): {total_reported}")

        batch = page.get("result")
        if not isinstance(batch, list) or not batch:
            break

        for client in batch:
            if isinstance(client, dict):
                clients.append(client)

        print(f"  страница {pages}: +{len(batch)} (накоплено {len(clients)})")

        next_offset = page.get("offset")
        if not isinstance(next_offset, int) or next_offset < 0:
            break
        offset = next_offset

        if args.pause > 0:
            time.sleep(args.pause)

    rows = build_rows(clients)
    with_email = sum(1 for r in rows if r["email"])
    write_csv(args.output, rows)

    print(f"Готово: {len(rows)} клиентов, с email: {with_email}")
    print(f"CSV: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

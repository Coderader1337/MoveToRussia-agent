"""
Только чтение: POST search по email в EnvyCRM (лиды и сделки).
Требуются переменные окружения:
  ENVYCRM_BASE_URL — корень инстанса, например https://вашакомпания.envycrm.com
  ENVYCRM_KEY      — ключ из Настройки → Интеграция → API

Формат API: POST JSON {"request": {"email": "..."}} на endpoints
/crm/api/v1/lead/search/ и /crm/api/v1/deal/search/ с query api_key=...
(как в публичных примерах EnvyCRM API v1).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from dotenv import load_dotenv

TARGET_EMAIL = "aluoranen@mail.ru"


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


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


def main() -> int:
    load_dotenv()
    base = _strip_wrapping_quotes(os.environ.get("ENVYCRM_BASE_URL", "")).rstrip("/")
    api_key = _strip_wrapping_quotes(os.environ.get("ENVYCRM_KEY", ""))

    if not base or not api_key:
        print(
            "Задайте ENVYCRM_BASE_URL и ENVYCRM_KEY в .env или окружении.",
            file=sys.stderr,
        )
        return 1

    q = urllib.parse.urlencode({"api_key": api_key})
    endpoints = {
        "lead_search": f"{base}/crm/api/v1/lead/search/?{q}",
        "deal_search": f"{base}/crm/api/v1/deal/search/?{q}",
    }
    body: dict[str, Any] = {"request": {"email": TARGET_EMAIL}}

    out: dict[str, Any] = {"email": TARGET_EMAIL, "results": {}}
    for name, url in endpoints.items():
        code, parsed = _post_json(url, body)
        out["results"][name] = {"http_status": code, "body": parsed}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

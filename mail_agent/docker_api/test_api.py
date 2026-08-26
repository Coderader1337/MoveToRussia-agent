"""
Тестовый скрипт для проверки работы Mail Agent API.

Секреты — только из docker_api/.env (не в git). Пример переменных:
  API_KEY=...
  TEST_MANAGER_EMAIL=manager@example.com
  TEST_MANAGER_PASSWORD=yandex_app_password
  TEST_CLIENT_EMAIL=client@example.com
  API_URL=http://localhost:8000
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
MANAGER_EMAIL = os.getenv("TEST_MANAGER_EMAIL", "")
MANAGER_PASSWORD = os.getenv("TEST_MANAGER_PASSWORD", "")
CLIENT_EMAIL = os.getenv("TEST_CLIENT_EMAIL", "")


def _imap_credentials_configured() -> bool:
    return bool(MANAGER_EMAIL and MANAGER_PASSWORD and CLIENT_EMAIL)


def test_health_check() -> bool:
    """Проверка работоспособности API."""
    print("[*] Проверка health check...")
    try:
        response = requests.get(f"{API_URL}/health", timeout=10)
        response.raise_for_status()
        data = response.json()
        print(f"[+] API работает: {data}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"[-] Ошибка health check: {e}")
        return False


def test_get_email_thread() -> bool:
    """Тест получения переписки."""
    if not _imap_credentials_configured():
        print(
            "\n[!] Пропуск теста переписки: задайте TEST_MANAGER_EMAIL, "
            "TEST_MANAGER_PASSWORD и TEST_CLIENT_EMAIL в docker_api/.env"
        )
        return True

    print(f"\n[*] Запрос переписки для клиента: {CLIENT_EMAIL}")

    headers = {
        "Content-Type": "application/json",
    }
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    payload = {
        "manager_email": MANAGER_EMAIL,
        "manager_password": MANAGER_PASSWORD,
        "client_email": CLIENT_EMAIL,
        "sent_mailbox": "Sent",
    }

    try:
        response = requests.post(
            f"{API_URL}/api/v1/emails/thread",
            json=payload,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        print(f"[+] Успешно получено писем: {data['total_count']}")
        print(f"   Клиент: {data['client_email']}")
        print(f"   Success: {data['success']}")

        if data["emails"]:
            print("\n[*] Первое письмо:")
            first = data["emails"][0]
            print(f"   Папка: {first['folder']}")
            print(f"   Тема: {first['subject']}")
            print(f"   От: {first['from']}")
            print(f"   Кому: {first['to']}")
            print(f"   Дата: {first['date']}")
            print(
                f"   Текст (первые 100 символов): {first['text_plain'][:100]}..."
            )

        output_file = Path("test_api_response.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[*] Полный ответ сохранен в: {output_file}")

        return True

    except requests.exceptions.HTTPError as e:
        print(f"[-] HTTP ошибка: {e}")
        if e.response is not None:
            print(f"   Статус: {e.response.status_code}")
            try:
                error_data = e.response.json()
                print(f"   Детали: {error_data}")
            except Exception:
                print(f"   Текст ответа: {e.response.text}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"[-] Ошибка запроса: {e}")
        return False


def test_api_key_validation() -> bool:
    """Тест проверки API ключа (если включена аутентификация)."""
    print("\n[*] Проверка валидации API ключа...")

    if not API_KEY:
        print("[!] API_KEY не задан в .env — аутентификация, вероятно, отключена")
        return True

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": "wrong_api_key",
    }

    payload = {
        "manager_email": MANAGER_EMAIL or "manager@example.com",
        "manager_password": MANAGER_PASSWORD or "placeholder",
        "client_email": CLIENT_EMAIL or "client@example.com",
    }

    try:
        response = requests.post(
            f"{API_URL}/api/v1/emails/thread",
            json=payload,
            headers=headers,
            timeout=10,
        )

        if response.status_code == 403:
            print("[+] Аутентификация работает корректно (403 Forbidden)")
            return True
        if response.status_code == 200:
            print(
                "[!] Предупреждение: API принял неверный ключ (проверьте API_KEY в .env)"
            )
            return False

        print(f"[!] Неожиданный статус код: {response.status_code}")
        return False

    except requests.exceptions.RequestException as e:
        print(f"[-] Ошибка запроса: {e}")
        return False


def main() -> int:
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    print("=" * 60)
    print("Тестирование MovetoRussia Mail Agent API")
    print("=" * 60)

    results: list[tuple[str, bool]] = []

    results.append(("Health Check", test_health_check()))

    if results[0][1]:
        results.append(("Get Email Thread", test_get_email_thread()))
        results.append(("API Key Validation", test_api_key_validation()))
    else:
        print("\n[!] Пропуск остальных тестов (API недоступен)")
        results.append(("Get Email Thread", False))
        results.append(("API Key Validation", False))

    print("\n" + "=" * 60)
    print("Результаты тестов:")
    print("=" * 60)
    for test_name, result in results:
        status = "[+] PASS" if result else "[-] FAIL"
        print(f"{status} - {test_name}")

    print("=" * 60)
    if all(result for _, result in results):
        print("[+] Все тесты пройдены успешно!")
        return 0

    print("[!] Некоторые тесты не прошли")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""
Тестовый скрипт для проверки работы Mail Agent API.
"""

import json
import sys
from pathlib import Path

import requests

# URL вашего API (измените на ваш IP если запускаете на другом компьютере)
API_URL = "http://localhost:8000"
API_KEY = "u_gnztg1VaWQov5DYFw1PpArfZ5xsAL0g3T_7FzUTDN27XjrqzKDf97sGppClc6B"

# Данные для теста (из .env)
MANAGER_EMAIL = "e.novik@arkvostok.com"
MANAGER_PASSWORD = "ukjnnagtatjuurpc"
CLIENT_EMAIL = "cluke92@icloud.com"


def test_health_check():
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


def test_get_email_thread():
    """Тест получения переписки."""
    print(f"\n[*] Запрос переписки для клиента: {CLIENT_EMAIL}")

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
    }

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

        # Сохранение результата в файл
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


def test_api_key_validation():
    """Тест проверки API ключа (если включена аутентификация)."""
    print("\n[*] Проверка валидации API ключа...")

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": "wrong_api_key",
    }

    payload = {
        "manager_email": MANAGER_EMAIL,
        "manager_password": MANAGER_PASSWORD,
        "client_email": CLIENT_EMAIL,
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
        elif response.status_code == 200:
            print(
                "[!] Предупреждение: API работает без аутентификации (API_KEY не установлен)"
            )
            return True
        else:
            print(
                f"[!] Неожиданный статус код: {response.status_code}"
            )
            return False

    except requests.exceptions.RequestException as e:
        print(f"[-] Ошибка запроса: {e}")
        return False


def main():
    """Запуск всех тестов."""
    # Устанавливаем UTF-8 для Windows консоли
    import sys
    import io
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    
    print("=" * 60)
    print("Тестирование MovetoRussia Mail Agent API")
    print("=" * 60)

    results = []

    # Test 1: Health check
    results.append(("Health Check", test_health_check()))

    # Test 2: Получение писем
    if results[0][1]:
        results.append(("Get Email Thread", test_get_email_thread()))
    else:
        print("\n[!] Пропуск остальных тестов (API недоступен)")
        results.append(("Get Email Thread", False))

    # Test 3: Проверка API ключа
    if results[0][1]:
        results.append(("API Key Validation", test_api_key_validation()))
    else:
        results.append(("API Key Validation", False))

    # Итоги
    print("\n" + "=" * 60)
    print("Результаты тестов:")
    print("=" * 60)
    for test_name, result in results:
        status = "[+] PASS" if result else "[-] FAIL"
        print(f"{status} - {test_name}")

    all_passed = all(result for _, result in results)
    print("=" * 60)
    if all_passed:
        print("[+] Все тесты пройдены успешно!")
        return 0
    else:
        print("[!] Некоторые тесты не прошли")
        return 1


if __name__ == "__main__":
    sys.exit(main())

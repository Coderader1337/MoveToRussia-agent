"""
Генерация безопасного API ключа для Mail Agent API.
"""

import secrets
import string


def generate_api_key(length: int = 64) -> str:
    """
    Генерирует криптографически безопасный случайный API ключ.

    Args:
        length: Длина ключа (по умолчанию 64 символа)

    Returns:
        Случайный API ключ
    """
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main():
    """Генерация и вывод API ключа."""
    # Устанавливаем UTF-8 для Windows консоли
    import sys
    import io
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    print("=" * 70)
    print("Генератор API ключа для MovetoRussia Mail Agent")
    print("=" * 70)
    print()

    # Генерируем ключ
    api_key = generate_api_key()

    print("Ваш новый API ключ:")
    print()
    print(f"    {api_key}")
    print()
    print("-" * 70)
    print()
    print("Инструкции:")
    print()
    print("1. Скопируйте ключ выше")
    print("2. Создайте/откройте файл .env в корне проекта")
    print("3. Добавьте строку:")
    print()
    print(f"   API_KEY={api_key}")
    print()
    print("4. Перезапустите Docker контейнер:")
    print()
    print("   docker-compose down")
    print("   docker-compose up -d")
    print()
    print("5. Используйте этот ключ в заголовке X-API-Key при запросах")
    print()
    print("ВАЖНО: Сохраните этот ключ в безопасном месте!")
    print("   Если потеряете, нужно будет сгенерировать новый.")
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()

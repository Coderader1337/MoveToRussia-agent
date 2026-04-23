# Экспорт писем из Яндекс почты

Скрипты для экспорта всех писем от station@alice.yandex.ru через IMAP.

## Установка зависимостей

```bash
pip install -r requirements.txt
```

## Использование

### Вариант 1: Экспорт в формате EML (export_emails.py)

1. Убедитесь, что файл `.env` содержит правильные данные:
   - `MAIL_ADRESS` - ваш email адрес
   - `KEY_NAME` - название ключа (не используется в скрипте, но можно оставить)
   - `MAIL_KEY` - пароль приложения для IMAP

2. Запустите скрипт:

```bash
python export_emails.py
```

3. Все письма будут сохранены в папке `exported_emails` в формате `.eml`

**Формат имена файлов:** `YYYY-MM-DD_HH-MM-SS_Тема_письма.eml`

### Вариант 2: Экспорт в формате JSON (export_emails_json.py)

1. Убедитесь, что файл `.env` содержит правильные данные

2. Запустите скрипт:

```bash
python export_emails_json.py
```

3. Письма будут сохранены в папке `exported_emails_json`:
   - Все письма в одном файле: `emails.json`
   - Каждое письмо в отдельном файле: `YYYY-MM-DD_HH-MM-SS_Тема.json`

**Формат JSON:**

```json
{
  "subject": "Тема письма",
  "from": "Отправитель <email@example.com>",
  "date": "Wed, 22 Apr 2026 11:28:23 +0300",
  "textPlain": "Декодированный текст письма в UTF-8"
}
```

### Вариант 3: Экспорт в Google Sheets (export_to_sheets.py) ✨

Загружает письма прямо в вашу Google таблицу с бесплатной OAuth аутентификацией.

#### Подготовка (один раз):

1. Перейдите на https://console.cloud.google.com/
2. Создайте новый проект
3. Включите **Google Sheets API** и **Google Drive API**
4. Перейдите в "Credentials" → "Create Credentials" → **OAuth 2.0 Client ID**
5. Выберите тип приложения: **Desktop**
6. Скачайте JSON файл и переименуйте его в `credentials.json`
7. Поместите файл `credentials.json` в папку вашего проекта

#### Использование:

1. Добавьте в `.env` файл:

```
MAIL_ADRESS=leshapalamarchuk@yandex.ru
KEY_NAME=mailagent
MAIL_KEY=wyfcykoufepmcbzj
GOOGLE_SHEET_ID=1xsaw1jDwkZPtlF8knfDx0mgidLsAhrsj8gZjHXBNzZk
```

2. Запустите скрипт:

```bash
python export_to_sheets.py
```

3. При первом запуске откроется браузер для аутентификации
4. Токен сохранится в файл `token.json` для последующих запусков
5. Все письма загрузятся в вашу Google таблицу

**Особенности:**
- ✅ Бесплатная OAuth аутентификация (без платного Google Cloud)
- ✅ Автоматически декодирует Base64 содержимое
- ✅ Очищает текст от артефактов (\r, \n)
- ✅ Конвертирует всё в UTF-8
- ✅ Создаёт красивую таблицу с заголовками
- ✅ Автоматически подгоняет ширину колонок

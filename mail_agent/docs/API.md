# Mail API (Docker API)

Кастомный REST-сервис (`docker_api/`), который отдаёт переписку менеджера с
клиентом по IMAP (Yandex Mail). Используется как HTTP-шаг из n8n workflow.
Полные оригинальные версии этой документации (немного избыточные и частично
дублирующие друг друга) остались в самой папке `docker_api/`: `API_README.md`,
`DEPLOYMENT.md`, `QUICKSTART.md`, `README_API.md`.
Этот файл — выжимка + актуальный статус.

> Ранее в git попадали реальные секреты в `SUCCESS.md` / `START_HERE.md` /
> `.env.generated` (удалены) и в примерах `QUICKSTART.md` / `test_api.py`
> (очищены). См. [`STATUS.md`](STATUS.md) п.1 — значения нужно отозвать;
> в истории git они остаются.

## Технологии

Python 3.12, FastAPI, Uvicorn (2 workers), Pydantic, `imaplib` (стандартная
библиотека), Docker + Docker Compose.

## Запуск

```bash
cd mail_agent/docker_api
cp .env.api.example .env         # задать свой API_KEY
docker-compose up -d --build
curl http://localhost:8000/health
```

Swagger UI: `http://localhost:8000/docs`, ReDoc: `http://localhost:8000/redoc`.

## Endpoints

### `GET /health`

Проверка живости, без аутентификации.

```json
{ "status": "healthy", "service": "MovetoRussia Mail Agent API" }
```

### `POST /api/v1/emails/thread`

Получить всю переписку между менеджером и клиентом (IMAP `FROM`/`TO`/`CC` по
папкам `INBOX` и папке исходящих).

**Заголовки:**

```
Content-Type: application/json
X-API-Key: <API_KEY>          # обязателен, если задан API_KEY в окружении
```

**Тело запроса:**

```json
{
  "manager_email": "manager@example.com",
  "manager_password": "yandex_app_password",
  "client_email": "client@example.com",
  "sent_mailbox": "Sent"
}
```

| Поле | Обязательное | Описание |
|---|---|---|
| `manager_email` | да | Email менеджера в Yandex Mail |
| `manager_password` | да | **Пароль приложения** Yandex (не основной пароль!) |
| `client_email` | да | Email клиента, по которому ищем переписку |
| `sent_mailbox` | нет (`Sent` по умолчанию) | Имя папки исходящих: `Sent` (англ.) или `Отправленные` (рус.) — зависит от локали ящика |

**Ответ:**

```json
{
  "success": true,
  "client_email": "client@example.com",
  "emails": [
    {
      "folder": "INBOX",
      "subject": "...",
      "from": "...",
      "to": "...",
      "date": "Mon, 06 May 2026 12:00:00 +0300",
      "text_plain": "..."
    }
  ],
  "total_count": 2,
  "error": null
}
```

Ошибки: `401` (ошибка IMAP-аутентификации), `403` (неверный `X-API-Key`),
`503` (IMAP Yandex вернул неполный результат после исчерпанных повторов — см.
ниже), `500` (прочие внутренние ошибки).

## Реализация (`docker_api/api/main.py`)

- **IMAP-соединение**: `imaplib.IMAP4_SSL(imap.yandex.ru:993)`, readonly-режим
  (`EXAMINE` через `select(..., readonly=True)`), `FETCH BODY.PEEK[]` — письма не
  помечаются прочитанными, флаги ящика не меняются.
- **Поиск писем**: по каждому из `FROM`/`TO`/`CC` отдельным `SEARCH`, результаты
  объединяются во множество ID без дублей.
- **Устойчивость к временным сбоям Yandex IMAP**: `SEARCH`/`SELECT` повторяются
  до `IMAP_SEARCH_RETRIES` раз (по умолчанию 20, пауза `IMAP_SEARCH_RETRY_DELAY_SECONDS`
  = 1.5с) при "временных" ошибках (backend error, timeout, too many и т.п.);
  постоянные ошибки (auth failed, invalid, permission denied) не повторяются.
  Если после всех попыток результат неполный — `503`, а не тихая потеря писем.
- **Извлечение текста**: сначала `text/plain`, затем fallback на `text/html` →
  regex-конвертация в текст; из ответа вырезается цитируемая история (`On ... wrote:`,
  `-----Original Message-----`, `From:`/`Sent:`/`To:`/`Subject:` и русские аналоги,
  строки, начинающиеся с `>`).
- **Фильтр направления письма**: письмо из `INBOX` учитывается только если
  `client_email` реально стоит в `From`; из папки исходящих — только если
  `manager_email` в `From` и `client_email` в `To`/`Cc`. Это отсекает случайные
  совпадения по поиску.
- **Логирование**: в stdout и в файл (`LOG_FILE_PATH`, по умолчанию
  `/app/logs/imap_debug.log`), без паролей; подробные логи по каждому IMAP-вызову
  (полезно для диагностики "тихих" сбоев Yandex).

## Конфигурация (переменные окружения контейнера)

См. `docker_api/.env.api.example`:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `API_KEY` | — (пусто = без аутентификации, только для локальной разработки) | Значение заголовка `X-API-Key` |
| `IMAP_SEARCH_RETRIES` | 20 | Число повторов SEARCH/SELECT при временной ошибке |
| `IMAP_SEARCH_RETRY_DELAY_SECONDS` | 1.5 | Пауза между повторами |
| `IMAP_SOCKET_TIMEOUT` | 60 | Таймаут IMAP-сокета, сек |

## Известные ограничения

- Только Yandex Mail (`imap.yandex.ru:993`); для других провайдеров нужно менять
  `IMAP_SERVER`/`IMAP_PORT` в коде.
- Сервис публикуется на `localhost:8000`; для доступа из n8n Cloud использовался
  ngrok-туннель (нестабильный, URL меняется при перезапуске) — см.
  [`STATUS.md`](STATUS.md) п.2.
- Нет rate limiting и нет постоянного HTTPS/домена "из коробки" — для прод-варианта
  нужен отдельный VPS + nginx + Let's Encrypt (см. пример конфигурации nginx в
  `docker_api/DEPLOYMENT.md`), что не было развёрнуто.

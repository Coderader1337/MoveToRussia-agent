# Mail API (Docker API)

Кастомный REST-сервис (`docker_api/`), который отдаёт переписку менеджера с
клиентом по IMAP (Yandex Mail). Используется как HTTP-шаг из n8n workflow.
Единственный актуальный документ по API; старые `API_README.md`, `DEPLOYMENT.md`,
`QUICKSTART.md`, `README_API.md` из `docker_api/` удалены (содержимое сведено сюда).

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

## Интеграция с n8n

HTTP Request node:

- **Method:** POST
- **URL:** `http://<host>:8000/api/v1/emails/thread` (локальный n8n — IP машины с
  Docker, не `localhost`; n8n Cloud — ngrok или VPS с HTTPS)
- **Headers:** `Content-Type: application/json`, `X-API-Key: <API_KEY>`
- **Body:** `manager_email`, `manager_password` (пароль приложения Yandex),
  `client_email`, `sent_mailbox` (опционально)

Пример workflow: `docker_api/n8n_workflow_api_integration.json`. Подробнее по
нодам — [`N8N_WORKFLOW.md`](N8N_WORKFLOW.md).

Пароли Yandex — только [пароли приложений](https://passport.yandex.ru/profile/access),
не основной пароль. Хранить в n8n Credentials.

## Управление контейнером

```bash
cd mail_agent/docker_api
docker-compose ps
docker-compose logs -f mail-agent-api
docker-compose restart
docker-compose down
docker-compose up -d --build    # после изменений кода
python test_api.py              # smoke-тест
```

Генерация API-ключа: `python generate_api_key.py`.

## Troubleshooting

| Симптом | Решение |
|---|---|
| `401` IMAP auth | Пароль приложения Yandex, не основной; полный email в `manager_email` |
| Папка `Sent` не найдена | Для русских ящиков: `"sent_mailbox": "Отправленные"` |
| `403` | Неверный `X-API-Key` |
| `503` | Yandex IMAP вернул неполный результат после всех повторов — см. логи |
| Порт 8000 занят | Сменить внешний порт в `docker-compose.yml`: `"8001:8000"` |
| n8n не достучался | IP хоста (не localhost), firewall Windows, одна сеть с API |

Логи IMAP: stdout контейнера и `LOG_FILE_PATH` (по умолчанию `/app/logs/imap_debug.log`).

## Production (VPS + HTTPS)

Для постоянного доступа из n8n Cloud (вместо ngrok):

1. VPS с Docker Compose, скопировать `docker_api/` и `.env`.
2. nginx reverse proxy + Let's Encrypt.
3. Firewall: открыть 443, закрыть прямой доступ к 8000 из интернета.

Пример nginx:

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

**ngrok (только для теста):** `ngrok http 8000` → URL в n8n; нестабильно, URL
меняется при перезапуске — см. [`STATUS.md`](STATUS.md) п.2.

## Известные ограничения

- Только Yandex Mail (`imap.yandex.ru:993`); для других провайдеров нужно менять
  `IMAP_SERVER`/`IMAP_PORT` в коде.
- Сервис по умолчанию на `localhost:8000`; production HTTPS не развёрнут.
- Нет rate limiting.

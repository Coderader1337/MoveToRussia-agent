# MovetoRussia Mail Agent API

API endpoint для получения email переписки через IMAP (Yandex Mail) для использования в N8N workflows.

## Быстрый старт

### 1. Настройка API ключа (опционально)

Для защиты API создайте файл `.env` в корне проекта:

```bash
cp .env.api.example .env
```

Отредактируйте `.env` и установите свой API ключ:

```
API_KEY=your_secure_random_key_here
```

Если не хотите использовать аутентификацию (только для локальной разработки), оставьте переменную пустой.

### 2. Запуск через Docker Compose

```bash
docker-compose up -d --build
```

API будет доступен по адресу: `http://localhost:8000`

### 3. Проверка работоспособности

```bash
curl http://localhost:8000/health
```

Ответ:
```json
{
  "status": "healthy",
  "service": "MovetoRussia Mail Agent API"
}
```

### 4. Документация API

Swagger UI доступен по адресу: `http://localhost:8000/docs`

ReDoc: `http://localhost:8000/redoc`

## Использование API

### Получение переписки

**Endpoint:** `POST /api/v1/emails/thread`

**Headers:**
```
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Request Body:**
```json
{
  "manager_email": "manager@example.com",
  "manager_password": "yandex_app_password",
  "client_email": "client@example.com",
  "sent_mailbox": "Sent"
}
```

**Параметры:**
- `manager_email` — email менеджера в Yandex Mail
- `manager_password` — **пароль приложения** Yandex (не основной пароль!)
- `client_email` — email клиента для поиска переписки
- `sent_mailbox` — название папки исходящих (`Sent` или `Отправленные`, по умолчанию `Sent`)

**Response:**
```json
{
  "success": true,
  "client_email": "client@example.com",
  "emails": [
    {
      "folder": "INBOX",
      "subject": "Question about relocation",
      "from": "client@example.com",
      "to": "manager@example.com",
      "date": "Mon, 06 May 2026 12:00:00 +0300",
      "text_plain": "Hello, I have a question..."
    },
    {
      "folder": "Sent",
      "subject": "Re: Question about relocation",
      "from": "manager@example.com",
      "to": "client@example.com",
      "date": "Mon, 06 May 2026 14:00:00 +0300",
      "text_plain": "Thank you for your question..."
    }
  ],
  "total_count": 2,
  "error": null
}
```

### Пример запроса с curl

```bash
curl -X POST "http://localhost:8000/api/v1/emails/thread" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key_here" \
  -d '{
    "manager_email": "manager@example.com",
    "manager_password": "yandex_app_password_here",
    "client_email": "client@example.com",
    "sent_mailbox": "Sent"
  }'
```

### Пример запроса с Python

```python
import requests

url = "http://localhost:8000/api/v1/emails/thread"
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "your_api_key_here"
}
data = {
    "manager_email": "manager@example.com",
    "manager_password": "app_password",
    "client_email": "client@example.com",
    "sent_mailbox": "Sent"
}

response = requests.post(url, json=data, headers=headers)
print(response.json())
```

## Интеграция с N8N

### Настройка HTTP Request Node

1. Добавьте **HTTP Request** node в ваш workflow
2. Настройте параметры:

**Authentication:** None (или Header Auth если используете API ключ)

**Request Method:** POST

**URL:** `http://your-computer-ip:8000/api/v1/emails/thread`

**Headers:**
```json
{
  "Content-Type": "application/json",
  "X-API-Key": "your_api_key_here"
}
```

**Body:**
```json
{
  "manager_email": "{{ $json.manager_email }}",
  "manager_password": "{{ $json.manager_password }}",
  "client_email": "{{ $json.client_email }}",
  "sent_mailbox": "Sent"
}
```

3. Результат будет доступен в `{{ $json.emails }}`

### Пример N8N Workflow структуры

```
[Trigger] → [Set Variables] → [HTTP Request to API] → [Process Emails] → [Next Step]
```

## Безопасность

### API Key

- Используйте сильный случайный ключ (минимум 32 символа)
- Не коммитьте `.env` файл в git
- Регулярно меняйте API ключ
- Используйте разные ключи для разных окружений

### Пароли Yandex

- **НИКОГДА** не используйте основной пароль от Yandex
- Используйте только **пароли приложений**: https://passport.yandex.ru/profile/access
- Создайте отдельный пароль приложения для этого API
- Храните пароли в N8N Credentials (encrypted)

### Сетевая безопасность

- Не открывайте порт 8000 в интернет
- Используйте только в локальной сети или через VPN
- Для production добавьте HTTPS (reverse proxy nginx)
- Используйте firewall правила для ограничения доступа

## Управление контейнером

### Просмотр логов

```bash
docker-compose logs -f mail-agent-api
```

### Остановка

```bash
docker-compose down
```

### Перезапуск

```bash
docker-compose restart
```

### Пересборка после изменений

```bash
docker-compose up -d --build
```

## Troubleshooting

### Ошибка аутентификации IMAP

**Проблема:** `401 Unauthorized - Ошибка IMAP аутентификации`

**Решение:**
1. Проверьте, что используется **пароль приложения**, а не основной пароль
2. Создайте новый пароль приложения: https://passport.yandex.ru/profile/access
3. Убедитесь, что имя пользователя — полный email адрес

### Не находит папку исходящих

**Проблема:** Предупреждение о папке `Sent`

**Решение:**
- Для русскоязычных ящиков используйте `"sent_mailbox": "Отправленные"`
- Для англоязычных используйте `"sent_mailbox": "Sent"`

### Контейнер не запускается

```bash
# Проверить логи
docker-compose logs mail-agent-api

# Проверить статус
docker-compose ps
```

### API не отвечает

```bash
# Проверить, что контейнер запущен
docker ps | grep mail-agent

# Проверить health check
curl http://localhost:8000/health
```

## Развитие

### Локальная разработка без Docker

```bash
cd api
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Тестирование

```bash
# Установите зависимости для тестов
pip install pytest httpx

# Запустите тесты
pytest tests/
```

## Лицензия

Внутренний проект MovetoRussia.com

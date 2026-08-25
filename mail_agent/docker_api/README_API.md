# MovetoRussia Mail Agent API

Кастомный API endpoint для получения email переписки из Yandex Mail через IMAP. Предназначен для интеграции с N8N workflows.

## 📁 Структура проекта

```
mail_agent/
├── api/                                    # Docker API приложение
│   ├── main.py                            # FastAPI приложение
│   ├── Dockerfile                         # Образ Docker
│   ├── requirements.txt                   # Python зависимости
│   └── .dockerignore                      # Исключения для Docker
├── docker-compose.yml                     # Оркестрация Docker
├── QUICKSTART.md                          # Быстрый старт
├── API_README.md                          # Полная документация API
├── DEPLOYMENT.md                          # Документация по развертыванию
├── test_api.py                           # Тестовый скрипт
├── generate_api_key.py                   # Генератор API ключей
├── n8n_workflow_api_integration.json     # N8N workflow для интеграции
├── export_client_context_to_sheets.py    # Старый скрипт (с CRM + Sheets)
├── export_client_thread_to_txt.py        # Старый скрипт (в txt)
└── .env.api.example                      # Пример конфигурации
```

## 🚀 Быстрый старт

### 1. Генерация API ключа (опционально)

```bash
python generate_api_key.py
```

Скопируйте сгенерированный ключ в `.env`:

```bash
API_KEY=ваш_сгенерированный_ключ
```

### 2. Запуск Docker контейнера

```bash
docker-compose up -d --build
```

### 3. Проверка работы

```bash
# Health check
curl http://localhost:8000/health

# Или через Python
python test_api.py
```

### 4. Использование

```bash
curl -X POST "http://localhost:8000/api/v1/emails/thread" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ваш_api_ключ" \
  -d '{
    "manager_email": "manager@example.com",
    "manager_password": "app_password",
    "client_email": "client@example.com"
  }'
```

## 📖 Документация

- **[QUICKSTART.md](QUICKSTART.md)** — быстрый старт за 5 минут
- **[API_README.md](API_README.md)** — полная документация API
- **[DEPLOYMENT.md](DEPLOYMENT.md)** — архитектура и развертывание
- **Swagger UI** — http://localhost:8000/docs

## 🔧 Технологии

- **Python 3.12** — язык программирования
- **FastAPI** — веб-фреймворк
- **Uvicorn** — ASGI сервер
- **Docker** — контейнеризация
- **Pydantic** — валидация данных
- **imaplib** — работа с IMAP

## 🔐 Безопасность

- ✅ API Key аутентификация
- ✅ Пароли приложений Yandex (не основной пароль)
- ✅ IMAP readonly режим (не меняет флаги писем)
- ✅ Непривилегированный Docker пользователь
- ✅ Логирование без чувствительных данных

## 📊 API Endpoints

### `GET /health`
Проверка работоспособности сервиса.

**Response:**
```json
{
  "status": "healthy",
  "service": "MovetoRussia Mail Agent API"
}
```

### `POST /api/v1/emails/thread`
Получение переписки между менеджером и клиентом.

**Request:**
```json
{
  "manager_email": "manager@example.com",
  "manager_password": "app_password",
  "client_email": "client@example.com",
  "sent_mailbox": "Sent"
}
```

**Response:**
```json
{
  "success": true,
  "client_email": "client@example.com",
  "emails": [...],
  "total_count": 10,
  "error": null
}
```

## 🔗 Интеграция с N8N

1. Импортируйте workflow: `n8n_workflow_api_integration.json`
2. Настройте переменные окружения в N8N
3. Замените IP адрес в HTTP Request node
4. Активируйте workflow

## ⚙️ Настройка

### Переменные окружения (`.env`)

```bash
# API ключ (опционально для локальной разработки)
API_KEY=your_secure_key_here
```

### Docker Compose конфигурация

Порт можно изменить в `docker-compose.yml`:

```yaml
ports:
  - "8001:8000"  # Внешний:Внутренний
```

Workers можно увеличить в `api/Dockerfile`:

```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

## 🐛 Troubleshooting

### Ошибка IMAP аутентификации

Используйте **пароль приложения**, а не основной пароль Yandex:
https://passport.yandex.ru/profile/access

### Не находит папку исходящих

Используйте `"sent_mailbox": "Отправленные"` для русских ящиков.

### Порт занят

Измените внешний порт в `docker-compose.yml`.

### Подробнее

См. раздел Troubleshooting в [API_README.md](API_README.md).

## 📈 Производительность

- Health check: ~10-20ms
- 10 писем: ~2-5 секунд
- 50 писем: ~5-15 секунд

## 🧪 Тестирование

```bash
# Все тесты
python test_api.py

# Только health check
curl http://localhost:8000/health

# Swagger UI (интерактивное тестирование)
open http://localhost:8000/docs
```

## 📝 Примеры использования

### Python

```python
import requests

response = requests.post(
    "http://localhost:8000/api/v1/emails/thread",
    json={
        "manager_email": "manager@example.com",
        "manager_password": "app_password",
        "client_email": "client@example.com"
    },
    headers={"X-API-Key": "your_api_key"}
)

emails = response.json()["emails"]
```

### N8N HTTP Request

```json
{
  "method": "POST",
  "url": "http://192.168.1.100:8000/api/v1/emails/thread",
  "headers": {
    "X-API-Key": "{{ $env.MAIL_AGENT_API_KEY }}"
  },
  "body": {
    "manager_email": "{{ $json.manager_email }}",
    "manager_password": "{{ $json.manager_password }}",
    "client_email": "{{ $json.client_email }}"
  }
}
```

## 🔄 Обновление

```bash
# Остановить
docker-compose down

# Обновить код
git pull

# Пересобрать и запустить
docker-compose up -d --build
```

## 📊 Мониторинг

```bash
# Логи в реальном времени
docker-compose logs -f

# Статус контейнера
docker-compose ps

# Использование ресурсов
docker stats movetorussia_mail_agent_api
```

## 🤝 Вклад в проект

Это внутренний проект MovetoRussia.com.

## 📞 Поддержка

При возникновении проблем:
1. Проверьте логи: `docker-compose logs`
2. Запустите тесты: `python test_api.py`
3. Проверьте документацию в `API_README.md`

## 📜 Лицензия

Внутренний проект MovetoRussia.com

---

**Статус проекта:** ✅ Production Ready

**Версия:** 1.0.0

**Последнее обновление:** 06.05.2026

# Развертывание Mail Agent API через Docker

## Архитектура решения

```
┌─────────────┐
│   N8N Cloud │
│   Workflow  │
└──────┬──────┘
       │ HTTP Request
       │ (POST /api/v1/emails/thread)
       ↓
┌──────────────────────────────────┐
│  Ваш компьютер (localhost)       │
│                                  │
│  ┌────────────────────────────┐ │
│  │  Docker Container          │ │
│  │  ┌──────────────────────┐  │ │
│  │  │  FastAPI App         │  │ │
│  │  │  Port: 8000          │  │ │
│  │  └──────────────────────┘  │ │
│  └────────────────────────────┘ │
│         │                        │
│         │ IMAP (993)            │
│         ↓                        │
└──────────────────────────────────┘
         │
         │ SSL/TLS
         ↓
┌──────────────────┐
│  Yandex Mail     │
│  IMAP Server     │
│  imap.yandex.ru  │
└──────────────────┘
```

## Компоненты решения

### 1. FastAPI приложение (`api/main.py`)
- REST API endpoint для получения переписки
- Работа с Yandex IMAP
- Валидация входных данных через Pydantic
- Безопасность через API ключи
- Health check endpoint

### 2. Docker контейнер (`api/Dockerfile`)
- Python 3.12 slim базовый образ
- Минимальные зависимости
- Непривилегированный пользователь
- Health check встроен
- Uvicorn с 2 workers

### 3. Docker Compose (`docker-compose.yml`)
- Простой запуск одной командой
- Автоматический перезапуск
- Логирование с ротацией
- Health check мониторинг
- Изолированная сеть

### 4. N8N интеграция
- HTTP Request node
- Обработка JSON ответов
- Форматирование для LLM
- Error handling

## Безопасность

### Уровень 1: API Key аутентификация
- Все запросы требуют `X-API-Key` заголовок
- Ключ хранится в переменной окружения
- Можно отключить для локальной разработки

### Уровень 2: Пароли приложений Yandex
- НЕ используем основной пароль
- Только пароли приложений (app passwords)
- Можно отозвать в любой момент
- Разные пароли для разных целей

### Уровень 3: Сетевая изоляция
- API доступен только из локальной сети
- Не открываем порты в интернет
- Docker network изолирован
- Firewall правила Windows

### Уровень 4: Конфиденциальные данные
- `.env` файл не коммитится в git
- Пароли передаются через N8N credentials (encrypted)
- Логирование без чувствительных данных

## Стабильность и надежность

### Health checks
- Встроенный `/health` endpoint
- Docker healthcheck каждые 30 секунд
- Автоматический перезапуск при падении

### Обработка ошибок
- Корректные HTTP статус коды
- Детальные сообщения об ошибках
- Graceful handling IMAP ошибок
- Timeout защита (60 секунд)

### Логирование
- JSON логи
- Ротация (максимум 3 файла по 10MB)
- Уровни: INFO, WARNING, ERROR
- Timestamp на всех событиях

### IMAP соединение
- SSL/TLS шифрование
- Readonly режим (EXAMINE вместо SELECT)
- BODY.PEEK вместо BODY (не меняет флаги)
- Корректное закрытие соединения

### Масштабирование
- 2 Uvicorn workers по умолчанию
- Можно увеличить в docker-compose.yml
- Поддержка параллельных запросов
- Нет shared state (stateless)

## Производительность

### Оптимизации
- Slim образ Python (меньше размер)
- Минимальные зависимости
- Кэширование pip пакетов при сборке
- Непривилегированный пользователь (безопасность)

### Типичное время ответа
- Health check: ~10-20ms
- Получение 10 писем: ~2-5 секунд
- Получение 50 писем: ~5-15 секунд

## Мониторинг

### Проверка статуса
```bash
# Статус контейнера
docker-compose ps

# Логи в реальном времени
docker-compose logs -f

# Последние 100 строк логов
docker-compose logs --tail=100

# Health check
curl http://localhost:8000/health
```

### Метрики Docker
```bash
# Использование ресурсов
docker stats movetorussia_mail_agent_api

# Информация о контейнере
docker inspect movetorussia_mail_agent_api
```

## Обслуживание

### Обновление кода

1. Остановите контейнер:
```bash
docker-compose down
```

2. Внесите изменения в код

3. Пересоберите и запустите:
```bash
docker-compose up -d --build
```

### Обновление зависимостей

Отредактируйте `api/requirements.txt` и пересоберите:
```bash
docker-compose up -d --build
```

### Очистка

Удалить контейнер и образ:
```bash
docker-compose down
docker rmi movetorussia_mail_agent_api
```

Удалить все неиспользуемые Docker данные:
```bash
docker system prune -a
```

## Интеграция с N8N Cloud

### Шаг 1: Определите ваш публичный IP

Если N8N Cloud должен обращаться к вашему API, нужен публичный доступ.

**Внимание:** Для production использования рекомендуется:
- Использовать VPS/облачный сервер
- Настроить nginx reverse proxy с HTTPS
- Использовать доменное имя
- Настроить firewall правила

### Шаг 2: Проброс портов (для локального тестирования)

**Вариант A: ngrok (рекомендуется для тестирования)**

```bash
# Установите ngrok: https://ngrok.com/
ngrok http 8000
```

Получите публичный URL типа `https://abc123.ngrok.io`

**Вариант B: Проброс портов на роутере (не рекомендуется)**

1. Войдите в админку роутера
2. Найдите раздел Port Forwarding
3. Пробросьте порт 8000 на ваш компьютер
4. Используйте публичный IP

⚠️ **Важно:** При использовании публичного доступа обязательно используйте сильный API ключ!

### Шаг 3: Настройка N8N workflow

В HTTP Request node используйте:
- Для локального N8N: `http://YOUR_LOCAL_IP:8000`
- Для N8N Cloud + ngrok: `https://YOUR_NGROK_URL`
- Для production: `https://your-domain.com`

## Развертывание на VPS (production)

Для production рекомендуется:

1. Арендовать VPS (например, DigitalOcean, AWS, Yandex Cloud)
2. Установить Docker и Docker Compose
3. Настроить nginx как reverse proxy
4. Получить SSL сертификат (Let's Encrypt)
5. Настроить firewall (ufw)
6. Использовать systemd для автозапуска

Пример nginx конфигурации:
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## FAQ

**Q: Можно ли использовать для нескольких менеджеров?**
A: Да, просто передавайте разные `manager_email` и `manager_password` в каждом запросе.

**Q: Сколько писем можно получить за раз?**
A: Нет жесткого лимита, но рекомендуется до 100-200 писем для одного запроса.

**Q: Можно ли использовать для других почтовых провайдеров?**
A: Код написан для Yandex, но можно легко адаптировать для Gmail, Mail.ru и др. (изменить IMAP_SERVER).

**Q: Безопасно ли хранить пароли в N8N?**
A: N8N хранит credentials в зашифрованном виде. Это безопаснее чем в plain text.

**Q: Что делать если IMAP блокирует частые запросы?**
A: Yandex редко блокирует, но можно добавить rate limiting в FastAPI или кэширование.

## Поддержка

При проблемах проверьте:
1. Логи Docker: `docker-compose logs`
2. Health check: `curl http://localhost:8000/health`
3. Тестовый скрипт: `python test_api.py`
4. Документацию Swagger: `http://localhost:8000/docs`

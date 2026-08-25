# Быстрый старт Mail Agent API

## Шаг 1: Подготовка

Убедитесь, что у вас установлен Docker Desktop и он запущен.

## Шаг 2: Настройка API ключа (опционально)

Если хотите защитить API ключом, создайте файл `.env` в корне проекта:

```bash
cp .env.api.example .env
```

И измените API_KEY на свой случайный ключ.

## Шаг 3: Запуск Docker контейнера

```bash
docker-compose up -d --build
```

Команда создаст и запустит контейнер в фоновом режиме.

## Шаг 4: Проверка работы

### Вариант 1: Через curl

```bash
curl http://localhost:8000/health
```

Должен вернуть:
```json
{"status":"healthy","service":"MovetoRussia Mail Agent API"}
```

### Вариант 2: Через Python скрипт

```bash
python test_api.py
```

Этот скрипт проверит:
- Health check
- Получение писем
- Валидацию API ключа

### Вариант 3: Через браузер

Откройте в браузере:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Шаг 5: Тестовый запрос

```bash
curl -X POST "http://localhost:8000/api/v1/emails/thread" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_secure_api_key_here_change_me" \
  -d '{
    "manager_email": "e.novik@arkvostok.com",
    "manager_password": "ukjnnagtatjuurpc",
    "client_email": "cluke92@icloud.com",
    "sent_mailbox": "Sent"
  }'
```

## Шаг 6: Интеграция с N8N

1. В N8N импортируйте workflow из файла `n8n_workflow_api_integration.json`
2. Настройте переменные окружения в N8N:
   - `MAIL_AGENT_API_KEY` — ваш API ключ
   - `YANDEX_APP_PASSWORD` — пароль приложения Yandex
3. В HTTP Request node замените `YOUR_COMPUTER_IP` на IP вашего компьютера
4. Активируйте workflow

## Управление контейнером

### Просмотр логов
```bash
docker-compose logs -f
```

### Остановка
```bash
docker-compose down
```

### Перезапуск
```bash
docker-compose restart
```

### Остановка и удаление контейнера
```bash
docker-compose down -v
```

## Получение IP адреса компьютера

### Windows (PowerShell)
```powershell
Get-NetIPAddress | Where-Object {$_.AddressFamily -eq "IPv4" -and $_.IPAddress -notlike "127.*"}
```

### Windows (CMD)
```cmd
ipconfig
```

Ищите строку "IPv4 Address" в разделе вашей сетевой карты (обычно начинается с 192.168.x.x).

### Linux/Mac
```bash
hostname -I
```

или

```bash
ifconfig | grep "inet " | grep -v 127.0.0.1
```

## Troubleshooting

### Контейнер не запускается

```bash
# Проверить статус
docker-compose ps

# Посмотреть логи
docker-compose logs
```

### Порт 8000 уже занят

Измените порт в `docker-compose.yml`:
```yaml
ports:
  - "8001:8000"  # Используйте другой внешний порт
```

### Не могу подключиться из N8N

- Проверьте, что используете правильный IP адрес (не localhost, а реальный IP)
- Проверьте firewall — Windows может блокировать подключения
- Убедитесь, что N8N и API находятся в одной сети

### Ошибки IMAP

- Проверьте, что используете **пароль приложения**, а не основной пароль
- Создайте новый пароль: https://passport.yandex.ru/profile/access

## Полная документация

Смотрите `API_README.md` для подробной документации.

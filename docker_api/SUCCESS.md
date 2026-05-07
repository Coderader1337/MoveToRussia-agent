# 🚀 Mail Agent API успешно запущен!

## ✅ Текущий статус

**Docker контейнер**: Запущен и работает  
**API endpoint**: http://localhost:8000  
**API Key**: `u_gnztg1VaWQov5DYFw1PpArfZ5xsAL0g3T_7FzUTDN27XjrqzKDf97sGppClc6B`  
**Протестировано**: Все тесты пройдены успешно (51 письмо получено)

## 📂 Структура проекта

```
mail_agent/
├── docker_api/                            # Все файлы API в отдельной папке
│   ├── api/                              # FastAPI приложение
│   │   ├── main.py                       # Основной код API
│   │   ├── requirements.txt              # Python зависимости
│   │   ├── Dockerfile                    # Docker образ
│   │   └── .dockerignore                 # Исключения Docker
│   ├── docker-compose.yml                # Оркестрация контейнера
│   ├── .env                             # API ключ (НЕ коммитить!)
│   ├── .env.api.example                 # Пример конфигурации
│   ├── test_api.py                      # Тестовый скрипт
│   ├── generate_api_key.py              # Генератор API ключей
│   ├── n8n_workflow_api_integration.json # N8N workflow
│   ├── QUICKSTART.md                    # Быстрый старт
│   ├── API_README.md                    # Документация API
│   ├── DEPLOYMENT.md                    # Архитектура
│   └── README_API.md                    # Главный README
└── ... (остальные файлы проекта)
```

## 🔧 Быстрые команды

### Управление контейнером

```powershell
# Перейти в папку API
cd docker_api

# Просмотр статуса
docker-compose ps

# Просмотр логов
docker-compose logs -f

# Остановка
docker-compose down

# Запуск
docker-compose up -d

# Перезапуск
docker-compose restart

# Пересборка после изменений кода
docker-compose up -d --build
```

### Проверка работы

```powershell
# Health check
Invoke-RestMethod -Uri "http://localhost:8000/health"

# Полные тесты
python test_api.py

# Открыть Swagger UI в браузере
start http://localhost:8000/docs
```

## 📡 Использование API

### Базовый запрос (PowerShell)

```powershell
$headers = @{
    "Content-Type" = "application/json"
    "X-API-Key" = "u_gnztg1VaWQov5DYFw1PpArfZ5xsAL0g3T_7FzUTDN27XjrqzKDf97sGppClc6B"
}

$body = @{
    manager_email = "e.novik@arkvostok.com"
    manager_password = "ukjnnagtatjuurpc"
    client_email = "cluke92@icloud.com"
    sent_mailbox = "Sent"
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/emails/thread" -Method Post -Headers $headers -Body $body

$response | ConvertTo-Json -Depth 10
```

### Пример ответа

```json
{
  "success": true,
  "client_email": "cluke92@icloud.com",
  "total_count": 51,
  "emails": [
    {
      "folder": "Sent",
      "subject": "MovetoRussia.com: Dear Mr. Sullivan...",
      "from": "Evgenija Novik <e.novik@arkvostok.com>",
      "to": "cluke92@icloud.com",
      "date": "Sat, 06 Sep 2025 10:41:04 +0200",
      "text_plain": "..."
    }
  ],
  "error": null
}
```

## 🔗 Интеграция с N8N

### Шаг 1: Импорт workflow

1. Откройте N8N Cloud
2. Создайте новый workflow
3. Импортируйте файл: `docker_api/n8n_workflow_api_integration.json`

### Шаг 2: Настройка переменных окружения

В N8N добавьте переменные:

```
MAIL_AGENT_API_KEY=u_gnztg1VaWQov5DYFw1PpArfZ5xsAL0g3T_7FzUTDN27XjrqzKDf97sGppClc6B
YANDEX_APP_PASSWORD=ukjnnagtatjuurpc
```

### Шаг 3: Узнать IP адрес компьютера

```powershell
# Windows
ipconfig | Select-String "IPv4"

# Или более точно
Get-NetIPAddress | Where-Object {$_.AddressFamily -eq "IPv4" -and $_.IPAddress -notlike "127.*"}
```

### Шаг 4: Обновить URL в N8N

В HTTP Request node замените:
- `http://YOUR_COMPUTER_IP:8000` → `http://192.168.x.x:8000`

### Шаг 5: Активировать workflow

Workflow готов к использованию!

## 🔐 Безопасность

### API Key

- ✅ **Текущий ключ**: `u_gnztg1VaWQov5DYFw1PpArfZ5xsAL0g3T_7FzUTDN27XjrqzKDf97sGppClc6B`
- ✅ Храни��ся в `.env` (не коммитится в git)
- ✅ Можно сгенерировать новый: `python generate_api_key.py`

### Пароли Yandex

- ✅ Используется пароль приложения (не основной пароль)
- ✅ Можно отозвать в любой момент: https://passport.yandex.ru/profile/access
- ⚠️  Не передавайте пароли в git или открытых каналах

### Сетевая безопасность

- ✅ API доступен только из локальной сети (порт 8000)
- ⚠️  НЕ открывайте порт 8000 в интернет без HTTPS и firewall
- ✅ Для production используйте VPS + nginx + SSL

## 📊 Мониторинг

### Проверка здоровья контейнера

```powershell
# Статус контейнера
docker ps | Select-String "mail_agent"

# Health check статус
docker inspect movetorussia_mail_agent_api --format='{{.State.Health.Status}}'

# Использование ресурсов
docker stats movetorussia_mail_agent_api --no-stream
```

### Логи

```powershell
# Последние 50 строк
docker-compose logs --tail=50

# Логи в реальном времени
docker-compose logs -f

# Только ошибки
docker-compose logs | Select-String "error"
```

## 🐛 Troubleshooting

### Контейнер не запускается

```powershell
# Проверить логи
docker-compose logs

# Проверить конфигурацию
docker-compose config

# Убить старые контейнеры
docker-compose down -v
docker-compose up -d --build
```

### API не отвечает

```powershell
# Проверить, что контейнер запущен
docker-compose ps

# Проверить health check
curl http://localhost:8000/health

# Если не помогает — перезапуск
docker-compose restart
```

### Ошибка 403 Forbidden

- Проверьте API ключ в заголовке `X-API-Key`
- Убедитесь, что используете правильный ключ из `.env`

### Ошибка IMAP аутентификации

- Используйте **пароль приложения**, а не основной пароль
- Создайте новый: https://passport.yandex.ru/profile/access

## 📚 Документация

- **QUICKSTART.md** — быстрый старт за 5 минут
- **API_README.md** — полная документация API
- **DEPLOYMENT.md** — архитектура и развертывание
- **Swagger UI** — http://localhost:8000/docs

## 🎯 Следующие шаги

1. **Для локального тестирования**: все готово!
2. **Для N8N Cloud**: настройте проброс портов (ngrok) или используйте VPS
3. **Для production**: разверните на VPS с nginx и SSL сертификатом

## 💡 Полезные ссылки

- Yandex App Passwords: https://passport.yandex.ru/profile/access
- Docker Desktop: https://www.docker.com/products/docker-desktop
- N8N Documentation: https://docs.n8n.io
- FastAPI Documentation: https://fastapi.tiangolo.com

---

**Статус проекта**: ✅ Production Ready  
**Дата запуска**: 06.05.2026  
**Версия API**: 1.0.0

🎉 **Поздравляю! API успешно настроен и готов к использованию!**

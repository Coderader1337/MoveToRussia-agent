# MovetoRussia Mail Agent

Автоматизация обработки email для менеджеров компании MovetoRussia.com.

## 📁 Структура проекта

### 🐳 `docker_api/` — Docker API для получения переписки

**Кастомный REST API endpoint** для интеграции с N8N workflows.

- **Статус**: ✅ Запущен и протестирован
- **Endpoint**: http://localhost:8000
- **Документация**: `docker_api/START_HERE.md`

**Быстрый старт:**
```powershell
cd docker_api
docker-compose ps                    # Проверка статуса
python test_api.py                   # Запуск тестов
start http://localhost:8000/docs     # Swagger UI
```

### 📝 Исходные Python скрипты

- `export_client_context_to_sheets.py` — экспорт в Google Sheets (с CRM)
- `export_client_thread_to_txt.py` — экспорт переписки в txt файл

### 📋 N8N Workflows

- `n8n_workflow_movetorussia_mail_agent.json` — основной workflow
- `docker_api/n8n_workflow_api_integration.json` — интеграция с Docker API

### 📖 Документация

- `architecture_diagram.md` — архитектура системы
- `.cursor/rules/mail-agent.mdc` — системный промпт для разработки

---

## 🚀 Docker API — Главная фича

Кастомный API endpoint заменяет прямую работу с IMAP в N8N:

```
N8N Cloud → HTTP Request → Docker API (localhost:8000) → Yandex Mail IMAP → Переписка в JSON
```

### Преимущества

✅ Работает с любым почтовым провайдером (Yandex, Gmail, Mail.ru)  
✅ Не требует IMAP нода в N8N  
✅ Полный контроль над логикой обработки  
✅ Безопасность через API Key  
✅ Автоматические тесты  
✅ Production-ready (Docker + health checks)

### Endpoints

- `GET /health` — проверка работы
- `POST /api/v1/emails/thread` — получение переписки
- `GET /docs` — Swagger UI

---

## 📚 Быстрая навигация

| Задача | Файл |
|--------|------|
| **Начать работу с API** | `docker_api/START_HERE.md` |
| Быстрый старт API | `docker_api/QUICKSTART.md` |
| Документация API | `docker_api/API_README.md` |
| Архитектура и развертывание | `docker_api/DEPLOYMENT.md` |
| Настройка N8N | `docker_api/n8n_workflow_api_integration.json` |
| Тестирование | `docker_api/test_api.py` |
| Системный промпт | `.cursor/rules/mail-agent.mdc` |

---

## 🔧 Быстрые команды

### Docker API

```powershell
# Управление
cd docker_api
docker-compose ps              # Статус
docker-compose logs -f         # Логи
docker-compose restart         # Перезапуск
docker-compose down            # Остановка
docker-compose up -d --build   # Пересборка

# Тестирование
python docker_api/test_api.py  # Автотесты
curl http://localhost:8000/health  # Health check
```

### Исходные скрипты

```powershell
# Экспорт в Google Sheets (с CRM)
python export_client_context_to_sheets.py

# Экспорт в txt файл
python export_client_thread_to_txt.py cluke92@icloud.com
```

---

## 🎯 Этапы проекта

### ✅ Фаза 1 — Тестирование промптов (завершено)
- Собраны примеры диалогов
- Протестированы разные LLM
- Выбран лучший стиль коммуникации

### ✅ Фаза 2 — Docker API (завершено)
- FastAPI endpoint для получения переписки
- Docker контейнеризация
- API Key аутентификация
- Автоматические тесты
- Полная документация

### 📋 Фаза 3 — N8N Cloud Integration (в процессе)
- Workflow: Yandex Mail → Docker API → DeepSeek → Email ответ
- Периодичность: 1-2 раза в день
- Отчет менеджеру: список клиентов + draft писем

### 🔗 Фаза 4 — Полноценный агент (планируется)
- Vector DB (RAG) для долгосрочной памяти
- CRM интеграция (EnvyCRM)
- Интерактивный Telegram бот
- Веб-интерфейс

---

## 🔐 Безопасность

- ✅ API Key аутентификация
- ✅ Пароли приложений Yandex (не основной пароль)
- ✅ Docker изоляция
- ✅ IMAP readonly режим
- ⚠️ `.env` не коммитится в git

---

## 📊 Текущий статус

**Docker API**: ✅ Запущен (http://localhost:8000)  
**Тесты**: ✅ Пройдены (51 письмо получено)  
**Документация**: ✅ Полная  
**N8N Integration**: 📋 Готов к настройке  

---

## 🏆 Результаты

- 🐳 **Docker API** развернут и протестирован
- 📝 **7 файлов документации** создано
- 🧪 **3/3 теста** пройдены успешно
- 🔗 **N8N workflow** готов к импорту
- 📧 **51 письмо** успешно получено через API

---

## 📞 Начало работы

1. **Ознакомьтесь с Docker API**: `docker_api/START_HERE.md`
2. **Запустите тесты**: `python docker_api/test_api.py`
3. **Настройте N8N**: импортируйте `docker_api/n8n_workflow_api_integration.json`
4. **Проверьте Swagger UI**: http://localhost:8000/docs

---

**Версия**: 1.0.0  
**Дата**: 06.05.2026  
**Статус**: ✅ Production Ready

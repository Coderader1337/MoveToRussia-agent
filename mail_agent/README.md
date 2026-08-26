# MovetoRussia Mail Agent (legacy)

> **Статус: legacy, на паузе.** Основной продукт компании — RAG-ассистент, который
> живёт отдельно в `../rag/` (см. `../rag/README.md` и `../README.md` в корне репозитория).
> Всё в этой папке — самостоятельная, более ранняя автоматизация (n8n + Google Sheets +
> Docker API), никак не связанная с RAG. Трогать только по явному запросу.

Автоматизация обработки email для менеджеров компании MovetoRussia.com через n8n.

Все команды ниже запускаются **из этой папки** (`cd mail_agent`) — скрипты ищут
`credentials.json` / `token.json` и другие файлы относительно текущей директории.

## 📁 Структура

### 🐳 `docker_api/` — Docker API для получения переписки

Кастомный REST API endpoint для интеграции с N8N workflows.

- **Endpoint**: http://localhost:8000
- **Документация**: `docker_api/START_HERE.md`

```powershell
cd docker_api
docker-compose ps                    # Статус
python test_api.py                   # Тесты
start http://localhost:8000/docs     # Swagger UI
```

### 📝 `scripts/` — legacy Python-скрипты

| Скрипт | Назначение |
|---|---|
| `export_client_context_to_sheets.py` | Экспорт переписки + CRM в Google Sheets |
| `export_client_thread_to_txt.py` | Экспорт переписки с одним клиентом в txt |
| `export_crm_client_emails_to_csv.py` | Выгрузка email клиентов из CRM в CSV |
| `deepseek_first_contact_to_sheets.py` | Черновик первого письма через DeepSeek → Sheets |
| `deepseek_rate_emails_in_sheets.py` | Оценка писем через DeepSeek в Sheets |
| `build_n8n_prompt.py` | Сборка промпта для n8n Code-ноды из `knowledge_base/movetorussia_agent_kb.md` + `prompts/communication_principles.txt` |
| `extract_kb_facts.py` | Разбить `knowledge_base/movetorussia_agent_kb.md` на чистую базу знаний + промпт для n8n-агента |
| `generate_architecture_diagram.py`, `generate_logical_diagram.py` | Генерация диаграмм N8N workflow (graphviz) → `docs/` |
| `deploy_vps_monitoring.py`, `vps_collect_stats.sh`, `vps_stats_summary.sh` | Мониторинг ресурсов VPS для legacy-контейнеров (`movetorussia_mail_agent_api`, `movetorussia_reply_bot`) |

Зависимости: `pip install -r requirements.txt` (gspread, google-auth, graphviz, python-dotenv).

```powershell
# Экспорт в Google Sheets (с CRM)
python scripts\export_client_context_to_sheets.py

# Экспорт в txt файл
python scripts\export_client_thread_to_txt.py cluke92@icloud.com
```

### 📋 N8N workflow

- `Mail Agent.json` — основной n8n workflow
- `docker_api/n8n_workflow_api_integration.json` — интеграция с Docker API

### 💬 `prompts/` — промпты и принципы коммуникации (legacy n8n-агент)

Отдельная копия принципов коммуникации для актуального RAG-ассистента лежит в
`../rag/prompt_data/communication_principles.txt` — файлы здесь её не переиспользуют.

### 📖 `docs/` — документация и диаграммы

Полная документация проекта (статус, Mail API, n8n workflow, скрипты,
инструкция «поднять с нуля», список доступов) — [`docs/README.md`](docs/README.md).
Начать с [`docs/STATUS.md`](docs/STATUS.md) — там же находка про секреты,
попавшие в git (см. раздел «⚠️ Безопасность» ниже).

- `architecture_diagram.md/.png/.svg` — архитектурная схема N8N workflow
- `logical_flow_diagram.md/.png/.svg` — логическая схема

### 🔐 Учётные данные (не в git)

- `credentials.json`, `token.json` — Google OAuth для Sheets-экспорта (перегенерировать через Google Cloud Console при необходимости)

### ⚠️ Безопасность: секреты, ранее попавшие в git

Ранее в `docker_api/SUCCESS.md`, `START_HERE.md`, `.env.generated` и примерах
в `test_api.py` / `QUICKSTART.md` были закоммичены реальные API-ключ и пароль
приложения Yandex. Эти файлы удалены или очищены, но **история git** всё ещё
содержит значения — их нужно отозвать. Подробности —
[`docs/STATUS.md`](docs/STATUS.md) (п.1) и [`docs/ACCESS_CHECKLIST.md`](docs/ACCESS_CHECKLIST.md).

---

## 🎯 Этапы проекта (история)

1. ✅ Тестирование промптов — примеры диалогов, выбор стиля коммуникации
2. ✅ Docker API — FastAPI endpoint для получения переписки, контейнеризация, тесты
3. 📋 N8N Cloud Integration — Yandex Mail → Docker API → DeepSeek → черновик письма
4. ➡️ Проект эволюционировал в RAG-ассистента (`../rag/`) — см. корневой `README.md`

## 🔧 Быстрые команды Docker API

```powershell
cd docker_api
docker-compose ps / logs -f / restart / down / up -d --build
python test_api.py
curl http://localhost:8000/health
```

# N8N workflow

В репозитории два n8n-файла — это **разные** сценарии, не версии друг друга.

## 1. `Mail Agent.json` (корень `mail_agent/`) — основной сценарий

Тот самый workflow, который реально гонялся в n8n Cloud (см. диаграммы —
[`architecture_diagram.md`](architecture_diagram.md),
[`logical_flow_diagram.md`](logical_flow_diagram.md) — сгенерированы из этого
файла скриптами `scripts/generate_*_diagram.py`).

### Триггер и общая схема

`Google Sheets Trigger` (проверка новой строки раз в минуту, таблица «Neznajka»)
→ извлечение email клиента → поиск в CRM EnvyCRM → загрузка карточки сделки →
определение почты ответственного менеджера → загрузка переписки по IMAP через
Docker API → сборка промпта → DeepSeek → запись черновика обратно в таблицу.

Подробный шаг-за-шагом разбор с ветвлениями ошибок — в
[`logical_flow_diagram.md`](logical_flow_diagram.md#пошаговое-описание).

### Ноды и что каждая делает

| Нода | Тип | Что делает |
|---|---|---|
| `Google Sheets Trigger` | `googleSheetsTrigger` | Опрос таблицы `11j3PD1d1Jzmq_L3XjUh60tflBgT7QWfva9EhPuf8AcM` ("Neznajka"), раз в минуту, событие `rowAdded`. Credential: `Google Sheets Trigger account` |
| `Code in Python` | `code` (python) | Берёт последнюю добавленную строку, достаёт `client_email` |
| `CRM Deal Lookup` | `httpRequest` | `POST {{ $vars.ENVYCRM_BASE_URL }}/crm/api/v1/deal/search/` с `api_key={{ $vars.ENVYCRM_KEY }}`, ищет сделки по email клиента |
| `Extract Deal ID` | `set` | Достаёт `deal_id` из первой найденной сделки |
| `CRM Deal Get` | `httpRequest` | `POST .../crm/api/v1/deal/get/` — полная карточка сделки по `deal_id` |
| `Extract Deal Data` | `set` | Достаёт `client_name`, `employee_id`, `client_nationality`, `first_message_from_client`, `stage_name` из карточки (кастомные поля CRM: `crm_1036419`, `crm_1035813`) |
| `Map Manager Email` | `code` (JS) | Словарь `employee_id → {email, mail_key}` (пример: `manager1@example.com`). Пароли — только из n8n Variables (`MANAGER1_MAIL_KEY` и т.д.), не в коде workflow |
| `IMAP Search` | `httpRequest` | `POST` на **Docker API** (см. [`API.md`](API.md)) — URL зашит как ngrok-туннель `https://dandy-caravan-tint.ngrok-free.dev/api/v1/emails/thread`, ключ `X-API-Key = {{ $vars.THREAD_API_KEY }}` |
| `extract last mail` | `code` (JS) | Последнее письмо с непустым текстом; если писем нет — fallback на `first_message_from_client` из CRM |
| `Format Email History` | `code` (JS) | Собирает всю переписку в хронологический текст с метками `FROM_CLIENT` / `TO_CLIENT` |
| `Assemble Prompt` | `code` (JS) | Собирает финальный промпт: системная инструкция (роль, стиль, 10-этапная воронка, правила "не выдумывать факты") + 3 эталонных примера писем + данные клиента + история переписки |
| `Basic LLM Chain` | `@n8n/n8n-nodes-langchain.chainLlm` | Отправляет `final_prompt` в LLM |
| `DeepSeek Chat Model` | `@n8n/n8n-nodes-langchain.lmChatDeepSeek` | Модель `deepseek-v4-pro`, `temperature=0.4`. Credential: `DeepSeek account` |
| `Update Google Sheets` | `googleSheets` (update) | Пишет обратно в ту же таблицу: `Neznajka_response` (текст письма), последнее письмо клиента, техническую ошибку (если была на любом из шагов `IMAP Search` / `Basic LLM Chain` / `CRM Deal Lookup` / `CRM Deal Get`). Credential: `Google Sheets account` |

### Обработка ошибок

- Нет email в строке → тихий стоп (без записи ошибки).
- CRM не нашла клиента/сделку → запись ошибки в таблицу, дальше не идём.
- Почта недоступна (`IMAP Search` упал) → сценарий **не останавливается**,
  продолжает с тем, что есть (fallback на CRM).
- DeepSeek не ответил → запись ошибки в таблицу, черновик не появляется.
- Ошибка сопоставления менеджера (`employee_id` не в словаре) → не блокирует,
  подставляется значение по умолчанию (`'0'`).

### Переменные n8n (`$vars`), которые нужно завести в n8n Cloud

`ENVYCRM_BASE_URL`, `ENVYCRM_KEY`, `THREAD_API_KEY`, `MANAGER1_MAIL_KEY`,
`MANAGER2_MAIL_KEY` (и по одной паре `MANAGERn_MAIL_KEY` на каждого менеджера,
добавленного в словарь `Map Manager Email`). Это переменные **n8n**, отдельные
от `.env`-файлов остального репозитория — их нужно завести заново в интерфейсе
n8n Cloud при повторном разворачивании.

### Credentials n8n, которые нужно создать заново

`Google Sheets account` (OAuth2), `Google Sheets Trigger account` (OAuth2, тот же
Google-аккаунт), `DeepSeek account` (API key). См.
[`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md).

## 2. `docker_api/n8n_workflow_api_integration.json` — вспомогательный пример

Отдельный, более простой webhook-based workflow (`Webhook Trigger` →
`Set Variables` → `HTTP Request` к Docker API). Судя по заглушке
`http://YOUR_COMPUTER_IP:8000` в URL и хардкоду `manager_email` по умолчанию —
это **шаблон/пример** интеграции для ручной настройки, а не тот сценарий, что
реально работал в n8n Cloud. Полезен как отдельная точка входа, если понадобится
дёрнуть Docker API из n8n напрямую по webhook, без Google Sheets и CRM.

## Как импортировать workflow в n8n

1. n8n Cloud (или self-hosted) → **Workflows → Import from File**.
2. Выбрать `Mail Agent.json` (или `docker_api/n8n_workflow_api_integration.json`).
3. Пересоздать credentials (см. выше) — они не экспортируются с секретами, только
   ссылки по `id`/`name`; после импорта нода будет требовать переподключения
   credential.
4. Завести переменные `$vars.*` (см. выше) в **Settings → Variables**.
5. Для `Mail Agent.json`: обновить URL в ноде `IMAP Search` на актуальный адрес
   Docker API (см. [`STATUS.md`](STATUS.md) п.2 про ngrok).
6. Активировать workflow.

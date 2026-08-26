# Скрипты (`mail_agent/scripts/`)

Все команды запускаются из папки `mail_agent/` (`cd mail_agent`) — скрипты ищут
`.env`, `credentials.json`, `token.json` относительно текущей директории.
Зависимости: `pip install -r requirements.txt` (`gspread`, `google-auth*`,
`graphviz`, `python-dotenv`) + `paramiko` для скриптов деплоя монитринга (не
входит в `requirements.txt` — ставить отдельно: `pip install paramiko`).

## Экспорт переписки и CRM

### `export_client_context_to_sheets.py`

По email клиента: читает EnvyCRM (`lead/search` + `deal/search`, read-only),
загружает переписку по IMAP (Yandex, readonly), пишет два листа в Google
Sheets — `CRM_Context` (поля сделки, важные для агента) и `Emails` (вся
переписка). Дополнительно сохраняет объединённый JSON в
`exported_emails_json/context_<email>.json`.

`.env`: `MAIL_ADRESS`, `MAIL_KEY` (ящик менеджера), `GOOGLE_SHEET_ID`,
`credentials.json`/`token.json` (Google OAuth), `ENVYCRM_BASE_URL`,
`ENVYCRM_KEY`, `CLIENT_EMAIL` (опционально), `IMAP_SENT_MAILBOX` (опционально).

```powershell
python scripts\export_client_context_to_sheets.py
```

### `export_client_thread_to_txt.py`

Та же переписка, но без CRM/Sheets — только в `.txt`-файл
(`exported_emails_txt/thread_<email>.txt`), плюс пытается вытащить "Вопрос
клиента" из CRM (по нескольким возможным ключам полей) как первое сообщение.

```powershell
python scripts\export_client_thread_to_txt.py client@example.com
python scripts\export_client_thread_to_txt.py -o my_thread.txt client@example.com
```

### `export_crm_client_emails_to_csv.py`

Полная постраничная выгрузка всех клиентов CRM (email + доп. контакты) в CSV.
Единственный вызываемый endpoint — `POST /crm/api/v1/client/list/`
(жёстко проверяется в коде, что путь не подменили на что-то другое — защита от
случайной записи).

```powershell
python scripts\export_crm_client_emails_to_csv.py
python scripts\export_crm_client_emails_to_csv.py --dry-run
python scripts\export_crm_client_emails_to_csv.py -o clients_emails.csv --page-size 100
```

## DeepSeek + Google Sheets (более ранние прототипы n8n-агента)

### `deepseek_first_contact_to_sheets.py`

Для клиентов конкретного менеджера CRM (`CRM_MANAGER_EMPLOYEE_ID`, по умолчанию
`1137861`) собирает CRM + первое письмо менеджера (самое раннее из ящиков
`e.novik`/`a.antonova`) → просит DeepSeek написать черновик первого письма →
пишет строку в Google Sheets (`client_email | deepseek_response | first_message
| first_email_from_manager`).

⚠️ **Не запускается в текущем состоянии репозитория** — импортирует
`mail_imap_utils.fetch_first_manager_email_to_client`, а файла
`mail_imap_utils.py` в репозитории нет (см. [`STATUS.md`](STATUS.md) п.5). Перед
использованием нужно восстановить/написать этот модуль.

`.env`: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_TEMPERATURE`,
`ENVYCRM_BASE_URL`, `ENVYCRM_KEY`, `CRM_MANAGER_EMPLOYEE_ID`,
`E_NOVIK_MAIL_ADRESS`, `E_NOVIK_MAIL_KEY`, `A_ANTONOVA_MAIL_ADRESS`,
`A_ANTONOVA_MAIL_KEY`, `IMAP_SENT_MAILBOX` (опц.), `DEEPSEEK_SHEET_ID`,
`credentials.json`/`token.json`.

```powershell
python scripts\deepseek_first_contact_to_sheets.py
python scripts\deepseek_first_contact_to_sheets.py --email client@example.com
python scripts\deepseek_first_contact_to_sheets.py --dry-run
```

### `deepseek_rate_emails_in_sheets.py`

Оценивает уже существующие письма в Google Sheets через DeepSeek (число 1–100):
отдельно черновик ИИ (`deepseek_response`) и письмо реального менеджера
(`first_email_from_manager`), без общего контекста — только критерии оценки +
первое сообщение клиента + одно письмо. Критерии — в
`prompts/deepseek_email_rating_criteria.txt`. Пишет лог в `deepseek_rating.log`.

```powershell
python scripts\deepseek_rate_emails_in_sheets.py
python scripts\deepseek_rate_emails_in_sheets.py --limit 5
python scripts\deepseek_rate_emails_in_sheets.py --only manager --force
python scripts\deepseek_rate_emails_in_sheets.py --dry-run
```

## Сборка промптов и базы знаний (n8n Code-нода)

### `build_n8n_prompt.py`

Собирает готовый JS-код для n8n Code-ноды универсального агента: системный
промпт (10-этапная воронка, правила "не выдумывать факты", запрет упоминать
ИИ) + база знаний (`../knowledge_base/movetorussia_agent_kb.md`, общая с RAG-
пайплайном) + принципы коммуникации (`prompts/communication_principles.txt`).
Результат: `prompts/n8n_universal_prompt.js`. Промпт целиком передаётся LLM
перед каждым письмом (устаревший блок с 3 эталонными примерами не используется
в этой версии — в отличие от `Assemble Prompt` внутри `Mail Agent.json`, где
примеры используются).

```powershell
python scripts\build_n8n_prompt.py
```

### `extract_kb_facts.py`

Разбивает `knowledge_base/movetorussia_agent_kb.md` (общий с RAG) на два файла:
`movetorussia_kb.md` (чистая база фактов: воронка, FAQ, факты, часовые пояса) и
`n8n_agent_prompt.md` (инструкции для ИИ-агента: роль, стиль, стоп-правила).
Секции пронумерованы (`## N. ...`), делятся по этим заголовкам до "## Приложение".

```powershell
python scripts\extract_kb_facts.py
python scripts\extract_kb_facts.py --only 1 2
```

## Диаграммы

### `generate_architecture_diagram.py`, `generate_logical_diagram.py`

Парсят `Mail Agent.json` (ноды и связи) и генерируют
`docs/architecture_diagram.{md,png,svg}` /
`docs/logical_flow_diagram.{md,png,svg}` через Graphviz. Нужен установленный
системный Graphviz (не только pip-пакет) — иначе рендер `.png`/`.svg` не
получится, останется только `.md` с mermaid-диаграммой.

```powershell
python scripts\generate_architecture_diagram.py
python scripts\generate_logical_diagram.py
```

## Мониторинг VPS

### `deploy_vps_monitoring.py`, `vps_collect_stats.sh`, `vps_stats_summary.sh`

Разворачивают на VPS (`207.244.254.188`, см. `.cursor/rules/mail-agent.mdc`) cron
(`*/5 * * * *`), который собирает статистику ресурсов для legacy-контейнеров
`movetorussia_mail_agent_api` и `movetorussia_reply_bot` в
`/opt/movetorussia/monitoring/`. Подключение по SSH через `paramiko`
(параметры — `--host`/`--user`/`--password` или переменные `SSH_HOST`,
`SSH_USER`, `SSH_PASSWORD`).

```powershell
python scripts\deploy_vps_monitoring.py
python scripts\deploy_vps_monitoring.py --run-now
```

## Что НЕ описано отдельно (вспомогательное/тестовое, внутри `docker_api/`)

`docker_api/test_api.py` (автотесты Docker API — содержит захардкоженные
секреты, см. [`STATUS.md`](STATUS.md) п.1), `docker_api/generate_api_key.py`
(генератор случайного 64-символьного ключа).

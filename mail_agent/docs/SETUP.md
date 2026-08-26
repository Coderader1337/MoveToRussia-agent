# Поднять Mail copilot с нуля

Пошаговая инструкция для человека, который никогда не разворачивал этот
legacy-проект. Если цель — рабочий RAG-ассистент, эта папка не нужна: см.
`../../rag/docs/SETUP.md`. Ниже — только legacy Mail copilot (n8n + Docker API +
Google Sheets).

## 0. Что вообще получится в итоге

Менеджер добавляет строку с email клиента в Google Sheets → раз в минуту n8n
подхватывает новую строку → тянет карточку клиента из CRM EnvyCRM → тянет
переписку менеджера с клиентом по IMAP через свой Docker API → просит DeepSeek
написать черновик ответа → записывает черновик обратно в таблицу.

Перед тем как начинать — прочитать [`STATUS.md`](STATUS.md): там честно
зафиксировано, что не протестировано и что нужно сначала подчистить
(скомпрометированные секреты, отсутствующий модуль, несовпадение имён `.env`).

## 1. Зависимости

```powershell
cd mail_agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Для мониторинга VPS дополнительно: `pip install paramiko`.
Для диаграмм — системный Graphviz (не только pip-пакет): https://graphviz.org/download/.
Для Docker API — Docker Desktop (Windows) или Docker Engine + Compose.

## 2. Доступы (сначала собрать, что понадобится)

Полный список — в [`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md). Минимально
нужны:

- Yandex-ящик(и) менеджера(ов) + **пароль приложения** (не основной пароль):
  https://passport.yandex.ru/profile/access
- Доступ к EnvyCRM (`ENVYCRM_BASE_URL`, `ENVYCRM_KEY`)
- Google-аккаунт с доступом к таблице "Neznajka" (Google Sheets) +
  `credentials.json` (OAuth Client ID, тип "Desktop app", Google Cloud Console)
- DeepSeek API-ключ (https://platform.deepseek.com)
- Аккаунт n8n Cloud (или self-hosted n8n)
- Если нужен внешний доступ к Docker API без своего VPS — аккаунт ngrok
  (временное решение, см. [`STATUS.md`](STATUS.md) п.2)

## 3. Настроить `.env`

Скрипты запускаются из `mail_agent/`, поэтому `.env` должен лежать именно там
(`mail_agent/.env`), даже если в корне репозитория уже есть свой `.env` для
RAG/data_pipeline — они не общие.

```powershell
# mail_agent/.env — минимальный набор для export_*-скриптов
MAIL_ADRESS=manager@yandex.ru
MAIL_KEY=<пароль приложения Yandex>
IMAP_SENT_MAILBOX=Sent          # или "Отправленные" для русских ящиков

GOOGLE_SHEET_ID=<id таблицы "Neznajka">

ENVYCRM_BASE_URL=<адрес EnvyCRM>
ENVYCRM_KEY=<ключ EnvyCRM>

DEEPSEEK_API_KEY=<ключ DeepSeek>
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TEMPERATURE=0.3

CLIENT_EMAIL=some_test_client@example.com   # опционально, дефолт для тестов
```

Для `deepseek_first_contact_to_sheets.py` дополнительно нужны переменные по
каждому менеджеру (`E_NOVIK_MAIL_ADRESS`/`_MAIL_KEY`,
`A_NOVIK_MAIL_ADRESS`/`_MAIL_KEY`, ...) и `CRM_MANAGER_EMPLOYEE_ID`,
`DEEPSEEK_SHEET_ID` — но сначала нужно восстановить отсутствующий модуль
`mail_imap_utils.py` (см. [`STATUS.md`](STATUS.md) п.5).

## 4. Google OAuth (`credentials.json` / `token.json`)

1. Google Cloud Console → создать проект (или использовать существующий) →
   включить Google Sheets API и Google Drive API.
2. **Credentials → Create Credentials → OAuth client ID → Desktop app** →
   скачать JSON → сохранить как `mail_agent/credentials.json`.
3. При первом запуске любого Sheets-скрипта откроется браузер для OAuth-логина
   — после подтверждения рядом появится `mail_agent/token.json` (автоматически,
   переиспользуется дальше, обновляется по `refresh_token`).
4. Оба файла — **не коммитить** (`credentials.json`, `token.json` уже в
   `.gitignore` проекта).

## 5. Docker API (получение переписки по IMAP)

```powershell
cd docker_api
python generate_api_key.py          # сгенерировать новый ключ (не использовать старый — он скомпрометирован, см. STATUS.md)
cp .env.api.example .env            # вписать API_KEY из шага выше
docker-compose up -d --build
curl http://localhost:8000/health
python test_api.py                  # секреты только из docker_api/.env (см. .env.api.example)
```

Подробности эндпоинта — [`API.md`](API.md).

## 6. Экспорт переписки локально (без n8n, для проверки доступов)

```powershell
cd mail_agent
python scripts\export_client_thread_to_txt.py client@example.com
```

Если файл `exported_emails_txt/thread_client_at_example.com.txt` появился и
непустой — IMAP и (если настроен CRM) EnvyCRM доступы работают.

## 7. n8n workflow

1. Завести аккаунт n8n Cloud (или self-hosted n8n).
2. Импортировать `mail_agent/Mail Agent.json`
   (Workflows → Import from File).
3. Пересоздать credentials: `Google Sheets account` / `Google Sheets Trigger
   account` (OAuth2 к тому же Google-аккаунту, что и таблица "Neznajka"),
   `DeepSeek account` (API key).
4. Settings → Variables — завести `ENVYCRM_BASE_URL`, `ENVYCRM_KEY`,
   `THREAD_API_KEY` (= `API_KEY` из Docker API), `E_NOVIK_MAIL_KEY`,
   `N_PERRY_MAIL_KEY` (пароли приложений Yandex по каждому менеджеру из словаря
   в ноде `Map Manager Email`).
5. Если Docker API не на постоянном публичном адресе — поднять туннель
   (`ngrok http 8000`) и вписать актуальный URL в ноду `IMAP Search` (см.
   [`N8N_WORKFLOW.md`](N8N_WORKFLOW.md)); для устойчивого решения — VPS с
   постоянным IP/доменом вместо ngrok.
6. Проверить/создать таблицу Google Sheets с листом, на который смотрит
   `Google Sheets Trigger` (структура столбцов — см.
   [`logical_flow_diagram.md`](logical_flow_diagram.md#что-попадает-в-таблицу)).
7. Активировать workflow, добавить тестовую строку с email клиента в таблицу,
   подождать до минуты (интервал опроса триггера) и проверить, что в той же
   строке появился черновик ответа.

## 8. (Опционально) Мониторинг VPS

Если Docker API или что-то ещё разворачивается на VPS —
`python scripts\deploy_vps_monitoring.py --run-now` (см.
[`SCRIPTS.md`](SCRIPTS.md)).

## 9. Диаграммы (если менялась структура `Mail Agent.json`)

```powershell
python scripts\generate_architecture_diagram.py
python scripts\generate_logical_diagram.py
```

## Чек-лист «всё готово»

- [ ] `docker-compose up` в `docker_api/` — `/health` отвечает `healthy`
- [ ] `export_client_thread_to_txt.py` вернул непустой файл переписки
- [ ] В n8n все три credential (`Google Sheets account`,
      `Google Sheets Trigger account`, `DeepSeek account`) подключены без
      ошибок
- [ ] Тестовая строка в Google Sheets получила черновик ответа в течение минуты
- [ ] Пароль приложения Yandex и API-ключ Docker API — **новые**, не те, что
      были закоммичены ранее (см. [`STATUS.md`](STATUS.md) п.1)

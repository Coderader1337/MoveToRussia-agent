# Статус проекта Mail copilot (legacy)

> Проект **legacy** и на паузе. Живой продукт компании — RAG-ассистент в `../../rag/`
> (см. `.cursor/rules/mail-agent.mdc`). Этот документ фиксирует состояние на момент
> паузы, чтобы через полгода можно было быстро понять, что тут работает, а что нет,
> без необходимости переспрашивать разработчика.

## Что готово и было протестировано вручную

| Компонент | Статус | Комментарий |
|---|---|---|
| **Docker API** (`docker_api/`) — FastAPI-сервис получения переписки по IMAP | ✅ Работал | Задокументированный прогон: health check + получение 51 письма реального клиента, ключ API проверен (403 при неверном ключе). См. `docker_api/SUCCESS.md`. |
| **n8n workflow `Mail Agent.json`** — полный сценарий (Sheets Trigger → CRM → почта → DeepSeek → черновик) | ⚠️ Собран и включает реальные ноды с привязанными credentials в n8n (`Google Sheets account`, `DeepSeek account`), но **не подтверждён end-to-end прогон с текущей конфигурацией** — сохранённых логов успешного полного цикла нет | Использует ngrok-туннель к Docker API (см. ниже, известное ограничение) |
| `export_client_context_to_sheets.py`, `export_client_thread_to_txt.py` | ✅ Рабочие, самодостаточные (CRM + IMAP, только чтение) | Ожидают `MAIL_ADRESS` / `MAIL_KEY` в `.env` — см. ⚠️ ниже про несовпадение имён переменных |
| `export_crm_client_emails_to_csv.py` | ✅ Рабочий, read-only (`client/list`) | — |
| `generate_architecture_diagram.py`, `generate_logical_diagram.py` | ✅ Рабочие, сгенерировали текущие диаграммы в `docs/` | Зависят от `graphviz` (Python-пакет + системный Graphviz) |

## Что не протестировано / известные ограничения

1. **⚠️ КРИТИЧНО — реальные секреты закоммичены в git.**
   В отслеживаемых файлах репозитория в открытом виде лежат настоящие credentials:
   - `docker_api/SUCCESS.md`, `docker_api/QUICKSTART.md`, `docker_api/START_HERE.md` —
     реальный `API_KEY` Docker API и реальный **пароль приложения Yandex** менеджера
     `e.novik@arkvostok.com`.
   - `docker_api/test_api.py` — те же значения зашиты прямо в код (константы
     `API_KEY`, `MANAGER_PASSWORD`).
   - `docker_api/.env.generated` — ещё один сгенерированный API-ключ в открытом виде.

   Это значит, что оба секрета (API-ключ Docker API и пароль приложения Yandex)
   **скомпрометированы самим фактом коммита** — даже если сейчас удалить их из
   рабочей копии, они остаются в истории git. **Владельцу нужно самостоятельно**:
   - отозвать пароль приложения Yandex для `e.novik@arkvostok.com`
     (https://passport.yandex.ru/profile/access) и создать новый (если ящик и API
     будут использоваться снова);
   - сгенерировать новый `API_KEY` для Docker API (`python generate_api_key.py`);
   - решить, стоит ли переписывать историю git (`git filter-repo` / BFG) для этого
     репозитория — это отдельная, потенциально разрушительная операция, которую
     агент не выполняет автоматически.
   Подробности и как избежать повтора — в [`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md).

2. **Docker API доступен наружу только через ngrok.** В ноде `IMAP Search` workflow
   `Mail Agent.json` зашит конкретный ngrok URL
   (`https://dandy-caravan-tint.ngrok-free.dev`). Бесплатные ngrok-туннели —
   временные: при перезапуске ngrok URL меняется, и ноду нужно обновлять вручную.
   Для реальной эксплуатации нужен постоянный адрес (VPS + домен/статический IP),
   который в проекте не был поднят (см. раздел «Production Deployment» в
   `docker_api/DEPLOYMENT.md` — остался на уровне рекомендаций, не реализован).

3. **Несовпадение имён переменных окружения почтового ящика.** Корневой `.env`
   хранит ключи по конкретному менеджеру (`E_NOVIK_MAIL_ADRESS`, `E_NOVIK_MAIL_KEY`,
   `A_ANTONOVA_MAIL_ADRESS`, `A_ANTONOVA_MAIL_KEY`, `N_PERRY_MAIL_ADRESS`,
   `N_PERRY_MAIL_KEY`), а часть скриптов (`export_client_context_to_sheets.py`,
   `export_client_thread_to_txt.py`) ожидают **обобщённые** `MAIL_ADRESS` / `MAIL_KEY`
   в `.env` **в папке `mail_agent/`** (которого сейчас нет — см. п.4). Перед запуском
   этих скриптов нужно вручную создать `mail_agent/.env` с нужным менеджерским
   ящиком под этими обобщёнными именами.

4. **Файла `mail_agent/.env` нет.** Скрипты в `mail_agent/scripts/` и
   `mail_agent/` вызывают `load_dotenv()` без пути — значит, ищут `.env` в текущей
   рабочей директории (`mail_agent/`), а не корневой `.env` репозитория. Часть
   переменных (`ENVYCRM_BASE_URL`, `ENVYCRM_KEY`, `GOOGLE_SHEET_ID`,
   `DEEPSEEK_API_KEY`) сейчас определена только в корневом `.env` — их нужно
   продублировать в `mail_agent/.env`, если запускать скрипты из этой папки.

5. **Отсутствует модуль `mail_imap_utils.py`.** `scripts/deepseek_first_contact_to_sheets.py`
   импортирует `from mail_imap_utils import fetch_first_manager_email_to_client`,
   но такого файла в репозитории нет — скрипт **не может быть запущен в текущем
   состоянии** без восстановления/написания этого модуля.

6. **`credentials.json` / `token.json` (Google OAuth) не в git** (правильно, но
   значит: у нового человека их не будет — см. `SETUP.md`, как получить заново).

7. **Мониторинг VPS** (`deploy_vps_monitoring.py`, `vps_collect_stats.sh`,
   `vps_stats_summary.sh`) — код готов, но нет зафиксированного подтверждения, что
   cron-задача сейчас активна на проде; относится к контейнерам
   `movetorussia_mail_agent_api` и `movetorussia_reply_bot`, которые сами по себе
   вне скоупа этой документации (см. `.cursor/rules/mail-agent.mdc`: "не трогать
   `docker_api`, `twitter_agent`" — там речь о **других**, актуальных контейнерах на
   том же VPS, не о legacy Mail Agent).

8. **`docker_api/n8n_workflow_api_integration.json`** — отдельный,
   **альтернативный** n8n workflow (webhook-триггер вместо Google Sheets Trigin),
   похоже, шаблон/пример, а не то, что реально запускалось в n8n Cloud (использует
   заглушку `YOUR_COMPUTER_IP`). Не путать с основным `Mail Agent.json` в корне
   `mail_agent/`.

## Итог по готовности

Легко воспроизводимо и рабочее «из коробки»: Docker API (без ngrok, локально) +
экспорт переписки/CRM в Sheets/txt/csv. Полный автоматический цикл через n8n
(Mail Agent.json) требует ручной настройки постоянного адреса для Docker API и
восстановления/проверки credentials в n8n — end-to-end не подтверждён
задокументированным прогоном.

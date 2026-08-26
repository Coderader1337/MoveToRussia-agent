# Список доступов и ключей (Mail copilot, legacy)

Только имена переменных/сервисов и где их получить — реальные значения не
хранятся в этом документе. Действующие значения — в `mail_agent/.env` (создать
самостоятельно, не в git), `docker_api/.env` (не в git) и в интерфейсе n8n
Cloud (Credentials / Variables).

## 1. Yandex Mail (IMAP)

| Что | Где получить/управлять |
|---|---|
| Ящик менеджера (например `manager@example.com`) | Стандартный логин Yandex |
| **Пароль приложения** (не основной пароль!) | https://passport.yandex.ru/profile/access → "Пароли приложений" |

Используется в: `MAIL_ADRESS`/`MAIL_KEY` (обобщённые скрипты),
`E_NOVIK_MAIL_ADRESS`/`_MAIL_KEY`, `A_ANTONOVA_MAIL_ADRESS`/`_MAIL_KEY`,
`N_PERRY_MAIL_ADRESS`/`_MAIL_KEY` (по менеджерам), Docker API запрос
`manager_password`, n8n-переменные `E_NOVIK_MAIL_KEY`/`N_PERRY_MAIL_KEY`.

- [ ] Есть доступ к почте Yandex каждого менеджера, задействованного в
      `Map Manager Email` (n8n)
- [ ] Для каждого — создан актуальный пароль приложения

## 2. EnvyCRM

| Переменная | Назначение |
|---|---|
| `ENVYCRM_BASE_URL` | Базовый URL CRM API |
| `ENVYCRM_KEY` | API-ключ CRM |

Управляется в личном кабинете EnvyCRM (запросить доступ у владельца аккаунта
CRM компании).

- [ ] Есть доступ к личному кабинету EnvyCRM, можно перегенерировать `ENVYCRM_KEY`

## 3. Google (Sheets + OAuth)

| Что | Где получить |
|---|---|
| `credentials.json` (OAuth Client ID, Desktop app) | Google Cloud Console → APIs & Services → Credentials |
| `token.json` | Генерируется автоматически при первом запуске скрипта (браузерный OAuth-логин) |
| `GOOGLE_SHEET_ID` | Из URL таблицы "Neznajka" |
| n8n credential `Google Sheets account` / `Google Sheets Trigger account` | OAuth2, тот же Google-аккаунт, настраивается прямо в n8n |

- [ ] Есть доступ к Google-аккаунту, на котором лежит таблица "Neznajka"
- [ ] Есть доступ к Google Cloud проекту (или можно создать новый) для выпуска
      `credentials.json`
- [ ] Известен `GOOGLE_SHEET_ID` таблицы "Neznajka"

## 4. DeepSeek

| Переменная | Назначение |
|---|---|
| `DEEPSEEK_API_KEY` | Ключ API DeepSeek (https://platform.deepseek.com) |
| n8n credential `DeepSeek account` | Тот же ключ, привязан к ноде `DeepSeek Chat Model` |

- [ ] Есть доступ к личному кабинету DeepSeek, можно перегенерировать ключ

## 5. Docker API (собственный сервис)

| Переменная | Назначение |
|---|---|
| `API_KEY` (`docker_api/.env`) | Ключ для заголовка `X-API-Key` при вызове `/api/v1/emails/thread` |
| `THREAD_API_KEY` (n8n variable) | То же значение, используется нодой `IMAP Search` |

Генерируется локально (`python docker_api\generate_api_key.py`) — не требует
внешнего аккаунта, но нужно синхронизировать значение между `docker_api/.env` и
n8n Variable `THREAD_API_KEY`.

- [ ] Ключ сгенерирован и совпадает в `docker_api/.env` и n8n Variable `THREAD_API_KEY`

## 6. n8n Cloud

| Что | Назначение |
|---|---|
| Аккаунт n8n Cloud (или self-hosted) | Хостинг workflow `Mail Agent.json` |
| Credentials: `Google Sheets account`, `Google Sheets Trigger account`, `DeepSeek account` | См. выше |
| Variables: `ENVYCRM_BASE_URL`, `ENVYCRM_KEY`, `THREAD_API_KEY`, `E_NOVIK_MAIL_KEY`, `N_PERRY_MAIL_KEY` | См. выше |

- [ ] Есть доступ к аккаунту n8n Cloud, на котором развёрнут workflow
- [ ] Активна подписка/план, достаточный для нужного числа выполнений workflow

## 7. ngrok (если Docker API не на постоянном сервере)

| Что | Назначение |
|---|---|
| Аккаунт ngrok | Публикация локального `localhost:8000` наружу для n8n Cloud |

Бесплатный тариф — временный URL, меняется при каждом перезапуске (см.
[`STATUS.md`](STATUS.md) п.1). Для постоянного решения — VPS с домен/статик IP
вместо ngrok.

- [ ] Известно, используется ли сейчас ngrok или Docker API уже перенесён на
      постоянный адрес

## 8. VPS-мониторинг (опционально)

| Переменная | Назначение |
|---|---|
| `SSH_HOST` (по умолчанию `207.244.254.188`), `SSH_USER` (`root`), `SSH_PASSWORD` | Доступ по SSH для `deploy_vps_monitoring.py` |

Тот же VPS, что описан в `.cursor/rules/mail-agent.mdc` (используется и RAG-
ассистентом, и legacy-контейнерами `movetorussia_mail_agent_api`,
`movetorussia_reply_bot`) — SSH-доступ общий, см. правило проекта.

- [ ] Есть SSH-доступ к VPS (по ключу или паролю — см. `.cursor/rules/mail-agent.mdc`)

## Итоговый чек-лист независимого доступа

- [ ] Yandex Mail — все нужные ящики менеджеров, пароли приложений
- [ ] EnvyCRM — личный кабинет
- [ ] Google — аккаунт с таблицей "Neznajka" + Cloud-проект для `credentials.json`
- [ ] DeepSeek — личный кабинет, API-ключ
- [ ] Docker API — `API_KEY` в `.env` и n8n
- [ ] n8n Cloud — аккаунт с workflow, все credentials переподключены
- [ ] (если используется) ngrok — аккаунт, либо подтверждено, что перешли на
      постоянный VPS-адрес
- [ ] (опционально) SSH к VPS для мониторинга

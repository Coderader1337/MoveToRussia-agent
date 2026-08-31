# Список ключей/доступов проекта + чек-лист независимого доступа

**В этом файле нет ни одного реального значения секрета** — только имена
переменных, где они используются и где их получить/сменить. Реальные
значения — в `.env` файлах (не в git) и в личном менеджере паролей владельца.

## 1. Полный список ключей и переменных окружения

### `rag/.env` (основной конфиг RAG-ассистента)

| Переменная | Назначение | Где получить/сменить |
|---|---|---|
| `VOYAGE_API_KEY` | Ключ Voyage AI (эмбеддинги) | https://www.voyageai.com/ — личный кабинет |
| `DEEPSEEK_API_KEY` | Ключ DeepSeek (LLM) | https://platform.deepseek.com/ — личный кабинет |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (прод) | @BotFather в Telegram |
| `VOYAGE_MODEL`, `VOYAGE_OUTPUT_DIMENSION`, `VOYAGE_BATCH_SIZE` | Настройки эмбеддингов, не секреты | `.env.example` |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_TEMPERATURE` | Настройки LLM, не секреты | `.env.example` |
| `QDRANT_URL` | Адрес Qdrant (`http://localhost:6333` на VPS) | не секрет |
| `QDRANT_API_KEY` | Ключ доступа к Qdrant, если включена авторизация | сейчас не используется (Qdrant слушает только `127.0.0.1`) |
| `QDRANT_COLLECTION` | Имя коллекции (`movetorussia_kb`) | не секрет |
| `MTR_CORPUS_PATH`, `MTR_FAQ_CSV_PATH` | Пути к данным, не секреты | `.env.example` |
| `YANDEX_DISK_TOKEN` | OAuth-токен Yandex Disk (опциональный слой "официальные файлы") | `scripts/get_yandex_disk_token.py` (нужны `YANDEX_OAUTH_CLIENT_ID/SECRET`) |
| `YANDEX_OAUTH_CLIENT_ID` / `YANDEX_OAUTH_CLIENT_SECRET` | OAuth-приложение Yandex (для получения токена выше) | https://oauth.yandex.ru — регистрация приложения, scope `cloud_api:disk.read` |
| `YANDEX_DISK_REMOTE_DIR` | Папка на Yandex Disk (`/rag_corpus`) | не секрет |
| `MTR_RETRIEVAL_TOP_K`, `MTR_DISK_RESERVE_SLOTS`, `MTR_DISK_MIN_SCORE` | Параметры retrieval, не секреты | `.env.example` |
| `MTR_HISTORY_TURNS`, `MTR_HISTORY_TTL_MIN` | Память диалога, не секреты | `.env.example` |
| `MTR_STATS_CSV_PATH`, `MTR_COMMUNICATION_PRINCIPLES_PATH`, `MTR_TELEGRAM_WHITELIST_PATH` | Пути (разные на VPS для prod/dev) | `.env.example`, `deploy/bootstrap-dev-instance.sh` |

### Корневой `.env` (используется легаси-скриптами `data_pipeline/`, `mail_agent/`)

| Переменная | Назначение | Где получить/сменить |
|---|---|---|
| `E_NOVIK_MAIL_ADRESS` / `E_NOVIK_MAIL_KEY` | IMAP-адрес и пароль приложения почтового ящика менеджера E. Novik | Yandex Почта — настройки → пароли приложений (Yandex 360) |
| `A_ANTONOVA_MAIL_ADRESS` / `A_ANTONOVA_MAIL_KEY` | То же для A. Antonova | Yandex Почта |
| `N_PERRY_MAIL_ADRESS` / `N_PERRY_MAIL_KEY` | То же для N. Perry | Yandex Почта |
| `IMAP_SENT_MAILBOX` | Опционально: имя папки "Отправленные", если не стандартное | не секрет |
| `DEEPSEEK_API_KEY` | Тот же ключ DeepSeek (fallback, если `rag/.env` не читается легаси-скриптами) | см. выше |
| `VPS_IP` / `VPS_PASS` | IP и пароль root на VPS (fallback-доступ, если SSH-ключ не работает) | у провайдера VPS |

### GitHub Actions Secrets (репозиторий → Settings → Secrets and variables → Actions)

| Секрет | Назначение | Где получить/сменить |
|---|---|---|
| `RAG_VPS_HOST` | IP VPS | у провайдера VPS |
| `RAG_VPS_USER` | `root` | — |
| `RAG_VPS_SSH_KEY` | Приватный SSH-ключ для деплоя (файл `rag/deploy/github_actions_deploy_key`, gitignored, публичная часть — `.pub`) | см. раздел "SSH на VPS" в `.cursor/rules/mail-agent.mdc` — переустановка ключа |

### Прочие доступы (не переменные окружения, но нужны для полного контроля)

| Доступ | Для чего | Примечание |
|---|---|---|
| SSH-доступ к VPS (`root@207.244.254.188`) | Деплой, диагностика, рестарт сервисов, доступ к Qdrant | Ключ `~/.ssh/id_ed25519` (Windows-путь разработчика) — см. правило `.cursor/rules/mail-agent.mdc`. Пароль root — fallback, см. `VPS_PASS` в `.env` |
| Аккаунт-владелец Telegram-бота (@BotFather) | Смена токена, настройка команд/описания бота, передача владения ботом | см. чек-лист ниже |
| Аккаунт Voyage AI | Биллинг, лимиты, ротация ключа | см. чек-лист ниже |
| Аккаунт DeepSeek | Биллинг, лимиты, ротация ключа | см. чек-лист ниже |
| Аккаунт Yandex (почта менеджеров + Yandex Disk + OAuth-приложение) | IMAP-доступ, официальные файлы | см. чек-лист ниже |
| Аккаунт хостинг-провайдера VPS | Биллинг, пересоздание сервера, IP | см. [`COSTS.md`](COSTS.md) |

## 2. Чек-лист независимого доступа (пункт 11 из чек-листа паузы)

Цель — убедиться, что через полгода **можно попасть во все аккаунты без
участия текущего разработчика**. Пройти по каждому пункту и отметить.

- [ ] **Telegram-бот (прод)** — есть логин/пароль (или 2FA-доступ) в аккаунт
  Telegram, который управляет ботом через @BotFather (владелец бота =
  Telegram-аккаунт, создавший его, а не токен). Если разработчик создавал
  бота со своего личного аккаунта — **передать владение** через @BotFather
  (`/mybots` → бот → Transfer Ownership) или изначально создать бота с
  аккаунта заказчика.
- [ ] **Telegram-бот (dev/test)** — то же самое для тестового бота, если он
  нужен на будущее (или можно не переносить — тестовый бот пересоздаётся
  бесплатно и быстро).
- [x] **Voyage AI** — аккаунт и ключ уже на стороне заказчика, ключ бессрочный,
  значение обновлено на VPS.
- [ ] **DeepSeek** — то же самое для аккаунта DeepSeek.
- [ ] **Yandex-почта менеджеров** (для IMAP-выгрузки) — доступ к самим
  ящикам (или хотя бы к паролям приложений) сохраняется независимо от
  разработчика — это корпоративная почта компании, обычно доступ уже есть у
  заказчика напрямую.
- [ ] **Yandex Disk / OAuth-приложение** (если используется опциональный
  слой) — доступ к аккаунту Yandex, на котором создано OAuth-приложение
  (`YANDEX_OAUTH_CLIENT_ID/SECRET`), и к самому Disk-хранилищу с файлами.
- [ ] **VPS-провайдер** — доступ к панели управления (аккаунт, привязанная
  карта/способ оплаты) независимо от разработчика — иначе продление сервера
  через полгода станет проблемой.

## 3. Где физически хранятся секреты сейчас

- `rag/.env`, `.env` (корень), `mail_agent/.env`, `mail_agent/docker_api/.env` —
  локальные файлы, **все в `.gitignore`**, не попадают в git.
- `rag/deploy/github_actions_deploy_key` (+ `.pub`) — локальный файл,
  gitignored (см. `rag/deploy/.gitattributes`).
- GitHub Actions Secrets — зашифрованное хранилище GitHub, видно только как
  имена в UI, значения нельзя посмотреть повторно (только пересоздать).
- `rag/bot/allowed_users.json` — список Telegram ID менеджеров. **Не в git**
  (`.gitignore`). Живёт на VPS; rsync его не затирает (`--exclude`).
  Шаблон формата: `rag/bot/allowed_users.example.json`.

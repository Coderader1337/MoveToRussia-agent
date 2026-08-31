# CI/CD и деплой

Подробный операционный README с командами — `rag/deploy/README.md`. Здесь —
общее объяснение, как это устроено и почему, плюс что деплоится, а что нет.

## Два инстанса на одном VPS

| Окружение | Путь на VPS | systemd-сервис | Ветка git | Telegram-бот |
|---|---|---|---|---|
| **prod** | `/opt/movetorussia/rag` | `movetorussia-rag-bot` | `master` | основной бот |
| **dev/test** | `/opt/movetorussia/rag-dev` | `movetorussia-rag-bot-dev` | `rag_develop` | `@Alpal_test_bot` (или см. фактический токен в `.env` VPS) |

Оба инстанса на одном VPS (`207.244.254.188`), делят один и тот же
self-hosted Qdrant (`localhost:6333`, коллекция `movetorussia_kb`) и одни и
те же API-ключи Voyage/DeepSeek. Отличаются только `TELEGRAM_BOT_TOKEN` и
путём к `usage_stats.csv` (см. `rag/deploy/bootstrap-dev-instance.sh`).

## Как срабатывает автодеплой

Два workflow в `.github/workflows/`:

- **`rag-deploy-dev.yml`** — триггер: push в `rag_develop` с изменениями в
  `rag/**`. Синкает `rag/` на `/opt/movetorussia/rag-dev/` (rsync **с
  `--delete`** — на dev-инстансе лишние файлы удаляются, чтобы окружение
  точно соответствовало ветке).
- **`rag-deploy-prod.yml`** — триггер: push в `master` (то есть после merge
  PR из `rag_develop`) с изменениями в `rag/**`. Синкает на
  `/opt/movetorussia/rag/` (rsync **без `--delete`** — чуть более
  консервативно для прода).

Оба workflow можно запустить и вручную (`workflow_dispatch`), и оба имеют
`concurrency: cancel-in-progress: true` — новый push отменяет предыдущий
незавершённый деплой той же ветки.

### Что происходит на каждый деплой

1. Checkout репозитория на GitHub-раннере.
2. Установка приватного SSH-ключа из секрета `RAG_VPS_SSH_KEY`.
3. `rsync -avz rag/ → <VPS>:<APP_DIR>/` с исключениями:
   - `venv/` (виртуальное окружение не деплоится, ставится на VPS)
   - `.env` (секреты никогда не приезжают через git/CI — только руками на VPS)
   - `data/` (usage_stats.csv, кэш Yandex Disk — данные конкретного инстанса)
   - `bot/allowed_users.json` (whitelist Telegram ID — живёт на VPS, не в git)
   - `__pycache__/`
   - `deploy/github_actions_deploy_key*` (приватный ключ деплоя не должен попасть на VPS через rsync)
4. По SSH на VPS: `chmod +x` на скрипты в `deploy/`, затем
   `deploy/install-and-restart.sh <APP_DIR> <SERVICE>`:
   - создаёт `venv/`, если его ещё нет;
   - `pip install -r requirements.txt`;
   - `systemctl daemon-reload && systemctl restart <SERVICE>`;
   - проверяет `systemctl is-active` и выводит последние строки логов.

### Что НЕ деплоится через CI (и почему)

- **`.env`** — секреты руками на VPS, никогда через git/Actions.
- **`venv/`** — создаётся на VPS при каждом деплое из `requirements.txt`.
- **`data/`** (`usage_stats.csv`, `yandex_disk/` кэш) — состояние конкретного
  инстанса, не должно перезатираться при деплое кода.
- **`bot/allowed_users.json`** — whitelist Telegram ID, правится на VPS,
  в git не коммитится.
- **Qdrant и его данные** — Qdrant не часть `rag/`, живёт отдельным
  Docker-контейнером на VPS, деплой бота его не трогает.
- **`mail_agent/`, `data_pipeline/`** — вообще не деплоятся, это
  локальные/легаси инструменты, workflow триггерится только на изменения в `rag/**`.

## Требуемые секреты GitHub Actions

Settings → Secrets and variables → Actions в репозитории:

| Секрет | Значение |
|---|---|
| `RAG_VPS_HOST` | IP-адрес VPS |
| `RAG_VPS_USER` | `root` |
| `RAG_VPS_SSH_KEY` | приватный ключ деплоя (файл `rag/deploy/github_actions_deploy_key`, локальный, gitignored) — публичный ключ уже должен быть в `/root/.ssh/authorized_keys` на VPS |

Подробнее про сам ключ и как его переустановить — [`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md).

## systemd на VPS

Бот запускается как systemd-сервис (не голым процессом/screen) — переживает
рестарт VPS и падения (`Restart=always`, `RestartSec=10`):

```
[Service]
WorkingDirectory=/opt/movetorussia/rag
EnvironmentFile=/opt/movetorussia/rag/.env
ExecStart=/opt/movetorussia/rag/venv/bin/python bot/telegram_bot.py
Restart=always
```

Файлы сервисов — `rag/deploy/movetorussia-rag-bot.service` (prod) и
`movetorussia-rag-bot-dev.service` (dev). Ручной перезапуск:

```bash
systemctl restart movetorussia-rag-bot        # прод
systemctl restart movetorussia-rag-bot-dev    # dev
journalctl -u movetorussia-rag-bot -n 50 --no-pager   # логи
```

## Отдельный таймер: синк Yandex Disk

Если используется опциональный слой "официальных файлов" (см.
[`DATA_PIPELINE.md`](DATA_PIPELINE.md)) — на **проде только** установлен
systemd-таймер `movetorussia-rag-disk-sync.timer`, раз в сутки в 00:00 МСК
запускает `movetorussia-rag-disk-sync.service` → `scripts/update_yandex_disk_corpus.py`.
Не связан с деплоем кода — работает независимо, установка/переустановка —
`bash rag/deploy/bootstrap-disk-sync.sh`.

## Первоначальная настройка нового окружения (dev или новый VPS)

Bootstrap-скрипты для случая, когда dev-инстанс (или весь VPS) нужно
поднять с нуля:

```bash
# 1. Разово создать структуру dev-инстанса, скопировать общие настройки из прода
bash rag/deploy/bootstrap-dev-instance.sh '<TELEGRAM_BOT_TOKEN для dev-бота>'

# 2. Первый деплой (дальше это делает CI автоматически)
bash rag/deploy/install-and-restart.sh /opt/movetorussia/rag-dev movetorussia-rag-bot-dev
```

Если поднимается **совсем новый VPS** — до этого нужно: установить Python 3.11+,
Docker (для Qdrant), синхронизировать код на сервер (вручную `rsync`/`scp` при
первом разе, раз ключ ещё не настроен в GitHub Secrets), создать `.env` руками,
поднять Qdrant (`docker compose up -d qdrant`, см. [`QDRANT.md`](QDRANT.md)),
проиндексировать данные, затем настроить systemd-сервисы как выше.

## Процесс релиза

- Разработка — в ветке `rag_develop`, автодеплой на dev-бота при каждом push.
- В прод — **только через Pull Request** `rag_develop → master` (см. правило
  в `.cursor/rules/mail-agent.mdc`); после merge автодеплой подхватывает прод.
- Ручной триггер деплоя (без нового коммита) — `workflow_dispatch` в GitHub
  Actions UI (Actions → выбрать workflow → Run workflow).

# Поднять проект с нуля

Общая схема: **данные → индекс (Qdrant) → бот**. Разделы 1–2 — подготовка
окружения, 3 — данные (можно пропустить, если готовый корпус уже есть в
репозитории/бэкапе), 4–6 — индексация и запуск.

## 0. Что нужно получить заранее

Прежде чем начинать, нужно раздобыть (см. подробный список в
[`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md)):

- Ключ **Voyage AI** (эмбеддинги).
- Ключ **DeepSeek** (LLM).
- Токен **Telegram-бота** (или создать новый через @BotFather).
- Доступ к **VPS** (или новый сервер, если старый утрачен) — для self-hosted Qdrant и деплоя бота.
- (Опционально) IMAP-пароли почтовых ящиков менеджеров — только если нужно
  пересобрать корпус переписки с нуля (раздел 3).
- (Опционально) токен **Yandex Disk** — только если нужен слой "официальных файлов" (раздел 3.4).

## 1. Локальное окружение

```powershell
git clone <URL репозитория>
cd mail_agent
python -m venv venv
venv\Scripts\activate
pip install -r rag\requirements.txt
```

Понадобится Python 3.11+ (используются `from __future__ import annotations`,
современный синтаксис типов `X | None`).

Для полной пересборки данных с нуля (раздел 3) также:

```powershell
pip install -r data_pipeline\requirements.txt
```

## 2. Секреты (`.env`)

```powershell
copy rag\.env.example rag\.env
notepad rag\.env
```

Заполнить как минимум:

```
VOYAGE_API_KEY=...
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
QDRANT_URL=http://localhost:6333
```

Остальные переменные — см. таблицу в [`rag/README.md`](../README.md#переменные-окружения-ragenv-см-envexample)
и комментарии в `rag/.env.example`. Полный список переменных и где их взять —
[`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md).

Если нужны легаси-скрипты `data_pipeline/` (IMAP-выгрузка, FAQ через DeepSeek) —
дополнительно создать `.env` в **корне репозитория** с IMAP-паролями
(`E_NOVIK_MAIL_ADRESS/KEY`, `A_ANTONOVA_MAIL_ADRESS/KEY`, `N_PERRY_MAIL_ADRESS/KEY`)
и `DEEPSEEK_API_KEY`.

## 3. Данные: получить корпус и FAQ

**Если готовые данные уже есть** (в репозитории — они gitignored, но могут
быть в бэкапе/архиве — см. [`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md) про
Yandex Disk / бэкапы) — просто положить их в корень репозитория и перейти к
разделу 4:

- `mailbox_export_RAG/corpus.jsonl` (~5900 chunks на момент паузы проекта)
- `knowledge_base/v4/client_faq_review.csv` (~224 вопроса)

**Если данных нет и нужно собрать с нуля** — полный цикл описан в
[`DATA_PIPELINE.md`](DATA_PIPELINE.md), кратко:

```powershell
cd mail_agent
python data_pipeline\export_manager_mailboxes.py      # 1. IMAP-выгрузка → mailbox_export/
python data_pipeline\clean_thread_quotes.py           # 2. очистка → mailbox_export_clean/
python data_pipeline\prepare_rag_corpus.py             # 3. корпус → mailbox_export_RAG/corpus.jsonl
python data_pipeline\build_faq_llm.py                  # 4. FAQ → knowledge_base/v4/client_faq_review.csv
```

Это может занять часы (тысячи писем, десятки/сотни вызовов DeepSeek) и
стоить денег (DeepSeek API) — см. [`COSTS.md`](COSTS.md).

**Важно:** не перегенерировать готовые данные без явной необходимости — это
дорого по времени/деньгам и меняет состав корпуса (см. предупреждение в
`data_pipeline/README.md`).

### 3.1 Опциональный слой Yandex Disk ("официальные файлы")

Если использовался daily-sync официальных файлов с Yandex Disk (`source=yandex_disk`,
наивысший приоритет в промпте) — см. раздел про Yandex Disk в
[`DATA_PIPELINE.md`](DATA_PIPELINE.md) и в `rag/deploy/README.md`. Это
опциональный слой, без него бот работает на корпусе почты + FAQ.

## 4. Qdrant (векторная база)

Self-hosted, поднимается в Docker (см. `rag/docker-compose.yml`):

```powershell
cd rag
docker compose up -d qdrant
```

Проверить: `http://localhost:6333/dashboard`. Подробности про схему коллекции
и восстановление — в [`QDRANT.md`](QDRANT.md).

## 5. Индексация корпуса в Qdrant

```powershell
cd rag
python scripts\index_corpus.py --dry-run     # оценить кол-во токенов/стоимость Voyage
python scripts\index_corpus.py --recreate    # первая полная индексация (создаёт коллекцию с нуля)
```

Полная индексация корпуса (~6000 chunks + ~225 FAQ) занимает от нескольких
минут до ~получаса — ограничение по RPM у Voyage API (см. `VOYAGE_BATCH_SIZE`,
`--sleep-between-batches`).

## 6. Проверка пайплайна (smoke test)

```powershell
python scripts\smoke_test.py
```

Индексирует ~20 chunks в отдельную локальную on-disk базу Qdrant (не трогает
основную коллекцию) и прогоняет пример вопроса + follow-up через весь пайплайн
retrieval → DeepSeek. Если Voyage API недоступен (гео-блок/сеть) — есть флаг
`--fake-embeddings` (только для проверки Qdrant/DeepSeek-цепочки, НЕ для
проверки качества поиска, см. `rag/README.md`).

## 7. Whitelist пользователей бота

Отредактировать `rag/bot/allowed_users.json` — список Telegram user ID,
которым разрешён доступ (см. `mtr_rag/whitelist.py`). Узнать свой Telegram ID
можно у бота [@userinfobot](https://t.me/userinfobot).

## 8. Запуск бота локально

```powershell
cd rag
python bot\telegram_bot.py
```

Открыть бота в Telegram, отправить `/start`, задать тестовый вопрос.

## 9. Деплой на сервер (продакшн)

Если нужен постоянно работающий бот — деплой на VPS через systemd + GitHub
Actions описан в [`CICD.md`](CICD.md) и `rag/deploy/README.md`. Ручной деплой
(без CI) — тоже возможен, см. `rag/deploy/install-and-restart.sh`.

## Контрольный список "всё готово"

- [ ] `rag/.env` заполнен реальными ключами (Voyage, DeepSeek, Telegram)
- [ ] `mailbox_export_RAG/corpus.jsonl` и `knowledge_base/v4/client_faq_review.csv` на месте
- [ ] Qdrant поднят и отвечает на `http://localhost:6333`
- [ ] `python scripts/index_corpus.py --recreate` прошёл без ошибок
- [ ] `python scripts/smoke_test.py` прошёл без ошибок (лучше — без `--fake-embeddings`)
- [ ] `rag/bot/allowed_users.json` содержит нужные Telegram ID
- [ ] Бот отвечает в Telegram на тестовый вопрос и на черновик письма

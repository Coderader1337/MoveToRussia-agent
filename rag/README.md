# MoveToRussia RAG Assistant

RAG-ассистент для менеджеров MoveToRussia.com: отвечает в Telegram на вопросы
по прецедентам из переписки с клиентами и по внутреннему FAQ, чтобы разгрузить
CEO от повторяющихся консультаций.

Индексация и обслуживание запросов — два независимых модуля, связанных только
через коллекцию Qdrant. `scripts/index_corpus.py` не импортирует ничего из
`bot/`, поэтому его легко переиспользовать как шаг n8n-воркфлоу инкрементальной
догрузки писем (n8n workflow сам по себе не создавался — по договорённости).

## Структура

```
rag/
├── mtr_rag/                      # Библиотека (без зависимости на бота)
│   ├── config.py                 # Settings — все переменные окружения
│   ├── schema.py                 # Chunk — единая модель (payload Qdrant)
│   ├── loaders.py                # corpus.jsonl / FAQ CSV / Yandex Disk → Chunk
│   ├── embeddings.py             # VoyageEmbedder (+ FakeDeterministicEmbedder для тестов)
│   ├── qdrant_store.py           # Коллекция, upsert, фильтры, поиск
│   ├── retriever.py              # LangChain BaseRetriever + приоритет yandex_disk
│   ├── question_extraction.py    # DeepSeek: извлечение вопросов из запроса менеджера
│   ├── mail_writing_prompt.py    # Системный промпт (Q&A / EMAIL DRAFT)
│   ├── chain.py                  # ask(question) → RagAnswer — вся цепочка
│   ├── whitelist.py              # allowed_users.json → множество Telegram ID
│   └── yandex_disk_sync.py       # Синк .txt с Yandex Disk → corpus.jsonl
├── scripts/
│   ├── index_corpus.py           # Индексация mailbox + FAQ в Qdrant
│   ├── smoke_test.py             # End-to-end проверка на маленькой выборке
│   ├── update_yandex_disk_corpus.py  # Синк Yandex Disk → Qdrant (systemd timer на проде)
│   └── get_yandex_disk_token.py  # One-time OAuth → YANDEX_DISK_TOKEN
├── bot/
│   ├── telegram_bot.py           # aiogram-бот: команды, форматирование ответа
│   ├── middleware.py             # Whitelist + обязательная оценка перед новым вопросом
│   ├── user_state.py             # FSM, история треда (follow-up), top_k на пользователя
│   └── stats.py                  # Append-only CSV usage_stats (оценки 1–10)
├── prompt_data/
│   └── communication_principles.txt
├── docs/                         # Подробная документация (см. docs/README.md)
├── .env.example
└── requirements.txt
```

Детальное описание каждого модуля — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Формат данных

**`mailbox_export_RAG/corpus.jsonl`** — по одному JSON-объекту на строку (chunk =
"exchange": вопрос(ы) клиента + ответ(ы) менеджера):

| Поле | Тип | Описание |
|---|---|---|
| `id` | str | Уникальный id chunk'а (`{thread_id}__ex{NNN}`) |
| `thread_id` | str | Id треда (файл в `mailbox_export_RAG/threads/`) |
| `client_email` | str | Email клиента |
| `manager_emails` | list[str] | Email(-ы) менеджера(-ов) в треде |
| `subject` | str | Тема письма |
| `date_start` / `date_end` | str (ISO 8601) | Диапазон дат exchange |
| `language` | str \| null | Язык (если определён) |
| `low_signal` | bool | Малоинформативный обмен (не удаляется, не приоритизируется) |
| `word_count` | int | Число слов в `text` |
| `text` | str | Очищенный текст переписки (`МЕНЕДЖЕР (дата): ...`) |
| `distilled` | str \| null | "Карточка знания" от DeepSeek (если запускали `--distill`) |
| `source` | str | `"mailbox_thread"` |

**FAQ-каталог** — `knowledge_base/v4/client_faq_review.csv` (`;`-разделитель),
колонки: `number;theme;theme_label;frequency;languages;question;question_original;answer;variants`.
Загружается в тот же `Chunk` со `source="faq_catalog"`, текст собирается как
`Q: {question}\nA: {answer}`.

**Yandex Disk** (опционально) — `data/yandex_disk/corpus.jsonl`, один `.txt`-файл
с Disk = один chunk со `source="yandex_disk"`. Считается самым авторитетным
источником при конфликте фактов. Локальное зеркало: `data/yandex_disk/files/`.
Синк и индексация — `scripts/update_yandex_disk_corpus.py` (на проде по systemd
timer, см. [`docs/DATA_PIPELINE.md`](docs/DATA_PIPELINE.md#опциональный-слой-yandex-disk-официальные-файлы)).
Без этого слоя бот работает на почте + FAQ.

## Переменные окружения (`rag/.env`, см. `.env.example`)

| Переменная | Назначение |
|---|---|
| `VOYAGE_API_KEY` | Ключ Voyage AI |
| `DEEPSEEK_API_KEY` | Ключ DeepSeek |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `VOYAGE_MODEL` | По умолчанию `voyage-4-large` |
| `VOYAGE_OUTPUT_DIMENSION` | По умолчанию `1024` |
| `VOYAGE_BATCH_SIZE` | Текстов на один embed-запрос, по умолчанию `64` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | По умолчанию `deepseek-v4-flash` |
| `DEEPSEEK_TEMPERATURE` | По умолчанию `0.2` |
| `QDRANT_URL` | Например `http://localhost:6333`. Для локального теста без сервера: `path:./_local_qdrant_db` |
| `QDRANT_API_KEY` | Если Qdrant защищён ключом |
| `QDRANT_COLLECTION` | По умолчанию `movetorussia_kb` |
| `MTR_RETRIEVAL_TOP_K` | top-k по умолчанию для retrieval (по умолчанию `6`) |
| `MTR_CORPUS_PATH` / `MTR_FAQ_CSV_PATH` | Пути к mailbox-корпусу и FAQ CSV (по умолчанию — относительно корня репозитория) |
| `YANDEX_DISK_TOKEN` | OAuth-токен Yandex Disk (опционально; получить через `scripts/get_yandex_disk_token.py`) |
| `YANDEX_OAUTH_CLIENT_ID` / `YANDEX_OAUTH_CLIENT_SECRET` | OAuth-приложение Yandex для получения `YANDEX_DISK_TOKEN` |
| `YANDEX_DISK_REMOTE_DIR` | Папка на Disk с `.txt`-файлами (по умолчанию `/rag_corpus`) |
| `MTR_DISK_CORPUS_DIR` | Локальное зеркало Yandex Disk (по умолчанию `data/yandex_disk`) |
| `MTR_DISK_RESERVE_SLOTS` | Сколько слотов в top-k резервировать под `yandex_disk` (по умолчанию `2`) |
| `MTR_DISK_MIN_SCORE` | Минимальный cosine score для резервирования disk-слота (по умолчанию `0.30`) |
| `MTR_HISTORY_TURNS` | Сколько пар вопрос/ответ хранить для follow-up (по умолчанию `10`) |
| `MTR_HISTORY_TTL_MIN` | Через сколько минут бездействия сбрасывать историю треда (по умолчанию `60`) |
| `MTR_TELEGRAM_WHITELIST_PATH` | Путь к `allowed_users.json` (по умолчанию `bot/allowed_users.json`) |
| `MTR_STATS_CSV_PATH` | CSV со статистикой оценок (по умолчанию `data/usage_stats.csv`; на prod/dev — разные пути) |
| `MTR_COMMUNICATION_PRINCIPLES_PATH` | Файл принципов общения для промпта (по умолчанию `prompt_data/communication_principles.txt`) |
| `REDIS_URL` | Redis для FSM при нескольких репликах бота (пусто = in-memory) |

## Запуск

```bash
cd rag
pip install -r requirements.txt
cp .env.example .env   # заполнить реальными ключами

# 1) Индексация (self-hosted Qdrant должен быть доступен по QDRANT_URL)
python scripts/index_corpus.py --dry-run          # оценить кол-во токенов/стоимость
python scripts/index_corpus.py --recreate          # первая полная индексация
python scripts/index_corpus.py                     # инкрементальный re-run (upsert по chunk id)

# 2) Быстрая проверка пайплайна на небольшой выборке (свой локальный Qdrant on-disk, без сервера)
python scripts/smoke_test.py

# 3) Telegram-бот
python bot/telegram_bot.py
```

### Флаги `scripts/index_corpus.py`

- `--recreate` — пересоздать коллекцию Qdrant с нуля.
- `--batch-size N` — размер батча для Voyage (учитывает лимит ~128 текстов/запрос).
- `--dry-run` — только считает токены (через локальный токенизатор Voyage) и
  оценивает стоимость, ничего не пишет в Qdrant и не делает embed-вызовов.
- `--no-faq` — индексировать только mailbox-корпус, без FAQ-каталога.
- `--limit N` — индексировать только первые N chunks (для смоук-тестов).
- `--sleep-between-batches SEC` — пауза между батчами Voyage (по умолчанию `2.0`, помогает не упереться в RPM).
- `--voyage-max-retries N` / `--voyage-base-delay SEC` — retry/backoff на 429/5xx от Voyage.

## Retrieval + генерация

`mtr_rag/chain.ask(question, top_k=..., source=..., exclude_low_signal=..., manager_email=...)`:

1. `question_extraction.py` — DeepSeek извлекает 1–8 фактических вопросов из запроса
   (или вставленного письма клиента), с учётом истории треда для разрешения ссылок.
2. Эмбеддинг каждого вопроса через Voyage (`input_type="query"`).
3. Top-k поиск в Qdrant (по умолчанию top_k=6); retriever резервирует до
   `MTR_DISK_RESERVE_SLOTS` слотов под `source=yandex_disk` при score ≥ `MTR_DISK_MIN_SCORE`.
4. Контекст собирается с явным указанием `thread_id` / `subject` / даты у
   каждого найденного фрагмента.
5. DeepSeek (`deepseek-v4-flash`) отвечает по `mail_writing_prompt.py` (Q&A или
   EMAIL DRAFT), с приоритетом источников: `yandex_disk` > `faq_catalog` > `mailbox_thread`.
6. Возвращается `RagAnswer(answer, sources, extracted_questions)`.

`low_signal` chunks не исключаются из индекса и не фильтруются по умолчанию —
только опционально через `exclude_low_signal=True` (использует Qdrant `must_not`
фильтр по полю `low_signal`).

## Известное ограничение тестовой среды

В песочнице, где собирался этот пайплайн, исходящие запросы к
`api.voyageai.com` блокировались на сетевом уровне (HTTP 403 без тела от
самого Voyage — похоже на гео-блок/сетевой фильтр, а не на проблему с ключом).
DeepSeek и HuggingFace (используется Voyage SDK для локальной токенизации в
`--dry-run`) были доступны без проблем.

Поэтому `scripts/smoke_test.py` поддерживает флаг `--fake-embeddings`
(детерминированный hash-эмбеддер вместо Voyage) — так проверены Qdrant
storage/retrieval и DeepSeek-генерация целиком, но **не** качество реального
семантического поиска Voyage. Перед вводом в эксплуатацию нужно прогнать
`python scripts/smoke_test.py` (без флага) в сети, где Voyage API доступен, и
убедиться, что ответы/источники релевантны.

## Документация для полного воспроизведения проекта

Подробные инструкции (поднять с нуля, пайплайн данных, архитектура, Qdrant,
CI/CD, поддержка, стоимость, доступы) — в [`docs/`](docs/README.md).

1. Инструкция «поднять проект с нуля»
rag/docs/SETUP.md

2. Пайплайн: почта → корпус → Qdrant, источник FAQ
rag/docs/DATA_PIPELINE.md

3. Архитектура кода
rag/docs/ARCHITECTURE.md

4. Qdrant: содержимое, пересборка, восстановление
rag/docs/QDRANT.md

5. CI/CD и деплой
rag/docs/CICD.md

6. Что требует разработчика и с какой частотой
rag/docs/MAINTENANCE.md

7. Оценка стоимости поддержки
rag/docs/COSTS.md

8. Полный список ключей и доступов
rag/docs/ACCESS_CHECKLIST.md

9. Независимый доступ к Telegram, Voyage, DeepSeek и Yandex Disk
rag/docs/ACCESS_CHECKLIST.md

# Архитектура кода RAG-ассистента

Описывает, что за что отвечает внутри `rag/`: библиотека `mtr_rag/`, бот
`bot/`, скрипты `scripts/`. Общая схема потока запроса и данных — в
[`rag/README.md`](../README.md#архитектура); здесь — детальнее по модулям.

## Принцип разделения

`mtr_rag/` — чистая библиотека без зависимости от Telegram/aiogram, её можно
использовать где угодно (в боте, в скриптах индексации, потенциально в n8n).
`bot/` зависит от `mtr_rag/`, но не наоборот. `scripts/` — CLI-обвязки вокруг
`mtr_rag/`, независимые от бота.

```
rag/
├── mtr_rag/                    # библиотека (без зависимости от бота)
│   ├── config.py               # Settings — все переменные окружения в одном месте
│   ├── schema.py                # Chunk — единая модель данных (payload Qdrant)
│   ├── loaders.py                # corpus.jsonl / FAQ CSV / Yandex Disk corpus → Chunk
│   ├── embeddings.py              # VoyageEmbedder (+ FakeDeterministicEmbedder для тестов)
│   ├── qdrant_store.py             # коллекция, upsert, delete, filter, search
│   ├── retriever.py                  # LangChain BaseRetriever + приоритет disk-источника
│   ├── question_extraction.py         # DeepSeek: извлечение фактических вопросов из запроса
│   ├── mail_writing_prompt.py          # системный промпт (Q&A / EMAIL DRAFT режимы)
│   ├── chain.py                          # ask(question) -> RagAnswer — вся цепочка целиком
│   ├── whitelist.py                       # allowed_users.json → множество Telegram ID
│   └── yandex_disk_sync.py                 # синк .txt с Yandex Disk → corpus.jsonl
├── bot/
│   ├── telegram_bot.py          # aiogram-бот: команды, обработчики, форматирование ответа
│   ├── middleware.py             # whitelist-гейт + "оцени перед новым вопросом"-гейт
│   ├── user_state.py              # FSM-состояние, история треда (follow-up), top_k на пользователя
│   └── stats.py                    # append-only CSV usage_stats.csv (по одной строке на оценку)
├── scripts/                     # CLI, независимые от бота
│   ├── index_corpus.py           # индексация corpus.jsonl (+FAQ) в Qdrant
│   ├── smoke_test.py              # end-to-end проверка на маленькой выборке (on-disk Qdrant)
│   ├── clone_qdrant_collection.py  # прод-safe клонирование коллекции (read-only на источнике)
│   ├── sync_missing_qdrant_points.py # донести недостающие points из одной коллекции в другую
│   ├── update_yandex_disk_corpus.py   # синк Yandex Disk → Qdrant (запускается таймером на проде)
│   ├── get_yandex_disk_token.py        # one-time обмен OAuth-кода на токен Yandex Disk
│   ├── prepare_telegram_corpus.py       # (экспериментальная ветка) подготовка Telegram-переписок как источника
│   ├── run_exp_batch.py / analyze_exp_comparison.py # инфраструктура для A/B сравнения промптов/параметров
│   └── _*.py                              # служебные разовые скрипты для деплоя/диагностики VPS (не часть пайплайна)
└── tests/
    └── test_disk_retrieval.py    # тест приоритета Yandex Disk источника в retriever
```

## `mtr_rag/config.py` — Settings

Единая точка входа для всех переменных окружения: `dataclass Settings`,
читает `os.environ` с дефолтами, `rag/.env` имеет приоритет над корневым
`.env` (оба подхватываются через `load_dotenv`). Все модули импортируют
`from .config import settings` — переменные окружения **нигде больше не
читаются напрямую**. Полная таблица переменных — `rag/README.md` и `.env.example`.

## `mtr_rag/schema.py` — Chunk

Единая модель данных для всех трёх источников (`mailbox_thread`,
`faq_catalog`, `yandex_disk`). `to_payload()`/`from_payload()` — сериализация
в/из Qdrant payload. Поле `extra: dict` хранит специфичные для источника поля
(`theme`/`frequency` для FAQ, `file_path`/`priority` для Yandex Disk).

## `mtr_rag/loaders.py`

Три функции-итератора (`iter_corpus_chunks`, `iter_faq_chunks`,
`iter_disk_corpus_chunks`) читают соответствующие файлы и возвращают `Chunk`.
`load_all_chunks()` — объединяет всё для индексации.

## `mtr_rag/embeddings.py`

`VoyageEmbedder` — обёртка над Voyage AI SDK, реализует интерфейс LangChain
`Embeddings` (`embed_documents`/`embed_query`/`embed_queries`). Особенности:
- Отдельный `input_type` для документов (`"document"`) и запросов (`"query"`) —
  важно для качества поиска у Voyage.
- Батчинг с retry/backoff на 429/5xx (Voyage free-tier ограничен по RPM).
- Глобальный `threading.Lock` вокруг вызова API — сериализует запросы.
- `count_tokens()` — для `--dry-run` оценки стоимости в `index_corpus.py`.

`FakeDeterministicEmbedder` — hash-based эмбеддер без реальной семантики,
только для offline smoke-теста, когда Voyage API недоступен.

## `mtr_rag/qdrant_store.py`

Тонкая обёртка над `qdrant-client`, без зависимости от LangChain — чтобы
`index_corpus.py` и потенциальный n8n-шаг могли использовать её напрямую.
- `get_client()` — поддерживает и self-hosted сервер (`QDRANT_URL=http://...`),
  и embedded on-disk режим (`QDRANT_URL=path:./some/dir`, используется в smoke test).
- `ensure_collection()` — создаёт коллекцию + payload-индексы (`source`,
  `thread_id`, `client_email`, `manager_emails`, `date_start`).
- `chunk_id_to_point_id()` — детерминированный UUID5 из строкового `id` чанка,
  чтобы повторная индексация того же chunk перезаписывала точку, а не дублировала.
- `upsert_chunks()`, `build_filter()`, `search()`, `reconcile_source()` (для
  удаления "осиротевших" точек при пересборке конкретного источника).

## `mtr_rag/retriever.py`

Кастомный LangChain `BaseRetriever` (не готовый `VectorStore`-wrapper — чтобы
фильтры по source/manager/дате напрямую маппились на свою схему
payload без слоя трансляции). Ключевая логика — `merge_disk_priority()`:
при обычном поиске (без явного `source`) отдельно запрашивается топ по
`source=yandex_disk` и резервируется до `MTR_DISK_RESERVE_SLOTS` слотов в
выдаче для релевантных (`score >= MTR_DISK_MIN_SCORE`) официальных файлов,
остальное — обычный топ по всем источникам.

## `mtr_rag/question_extraction.py`

Перед retrieval менеджерский запрос (или вставленное письмо клиента)
прогоняется через DeepSeek, который извлекает 1-8 самостоятельных фактических
вопросов на английском (для лучшего поиска) — учитывая историю треда только
для разрешения ссылок ("а по времени?"), не как источник фактов. Fallback —
если LLM не вернул валидный JSON-массив, используется исходный текст запроса целиком.

## `mtr_rag/mail_writing_prompt.py`

Системный промпт для генерации: определяет два режима —
**Q&A MODE** (прямой факт-вопрос менеджера) и **EMAIL DRAFT MODE** (менеджер
вставил письмо клиента и просит черновик ответа). Содержит:
- приоритет источников при конфликте фактов (`official_file` > `faq_catalog` > `mailbox_thread`);
- жёсткие правила ("не выдумывать факты", "не упоминать ИИ/RAG", "не давать
  письменных гарантий по визам", "не обещать бронирование отелей/билетов");
- загружает `prompt_data/communication_principles.txt` (тон, стиль, чувствительные темы).

## `mtr_rag/chain.py` — `ask()`

Собирает всё воедино: `extract_rag_questions()` → `retrieve_merged()` (поиск
по каждому извлечённому вопросу, дедупликация и слияние по лучшему score) →
промпт (`mail_writing_prompt` + история треда + контекст) → DeepSeek →
`sanitize_answer()` (убирает markdown-разметку, которую модель иногда всё
равно добавляет) → `RagAnswer(answer, sources, extracted_questions)`.

## `bot/telegram_bot.py` + вспомогательные модули

- **Поток одного запроса:** whitelist-проверка → `PendingRatingGateMiddleware`
  (блокирует новый запрос, если предыдущий ответ ещё не оценён) →
  `handle_question` → `chain.ask()` в отдельном потоке (`asyncio.to_thread`,
  чтобы не блокировать event loop) → ответ разбивается на 1-2 сообщения
  (анализ / черновик письма + Sources) → клавиатура с оценкой 1-10 →
  `append_usage_row()` → память треда (`append_history_turn`).
- **`user_state.py`** — история треда (follow-up вопросы) в памяти процесса
  (`MemoryStorage`, TTL `MTR_HISTORY_TTL_MIN`, окно `MTR_HISTORY_TURNS`),
  плюс FSM-состояние "ожидает оценку". Для нескольких реплик бота
  одновременно — можно переключить на Redis (`REDIS_URL`), но история треда
  всё равно в памяти процесса (не персистентна при рестарте бота).
- **`middleware.py`** — два middleware: `WhitelistMiddleware` (доступ только
  по `allowed_users.json`) и `PendingRatingGateMiddleware` (обязательная
  оценка 1-10 перед следующим запросом — это ключевая метрика проекта).
- **`stats.py`** — append-only CSV (`timestamp, telegram_id, question, answer, rate`)
  в `data/usage_stats.csv` (путь настраивается `MTR_STATS_CSV_PATH`, у dev и
  prod инстансов разные файлы).

## `scripts/` — CLI-обвязки

- **`index_corpus.py`** — единственный обязательный для полного цикла скрипт;
  флаги `--dry-run`/`--recreate`/`--limit`/`--no-faq`/`--batch-size` (см.
  подробнее в [`SETUP.md`](SETUP.md) и [`QDRANT.md`](QDRANT.md)).
- **`smoke_test.py`** — обязателен перед вводом в эксплуатацию после
  значимых изменений (проверяет retrieval + генерацию + память треда целиком).
- **`clone_qdrant_collection.py`** / **`sync_missing_qdrant_points.py`** —
  прод-safe копирование коллекции (read-only на источнике) — использовались
  для экспериментов (клонирование прод-базы в отдельную для A/B) и
  переноса telegram-экспериментальных данных в прод.
- **`update_yandex_disk_corpus.py`**, **`get_yandex_disk_token.py`** — см.
  [`DATA_PIPELINE.md`](DATA_PIPELINE.md#опциональный-слой-yandex-disk-официальные-файлы).
- **`run_exp_batch.py`**, **`analyze_exp_comparison.py`**, **`prepare_telegram_corpus.py`** —
  инфраструктура одного эксперимента (добавление Telegram-переписок как
  источника прецедентов и A/B сравнение ответов до/после) — не часть
  обязательного пайплайна, оставлены как задел, если понадобится повторить
  подобный эксперимент.
- **Скрипты с префиксом `_` (`_vps_*.py`, `_deploy_bot.py`, `_ci_deploy_dev_key.py` и т.п.)** —
  разовые служебные утилиты, которые использовались при первоначальной
  настройке VPS/CI (bootstrap SSH-ключей, диагностика деплоя). Не требуются
  для повторного разворачивания при рабочем CI/CD — оставлены как справочный
  материал на случай похожей проблемы в будущем.

## Тесты

`tests/test_disk_retrieval.py` — проверяет, что `merge_disk_priority()`
корректно резервирует слоты под официальные файлы Yandex Disk. Запуск:
`pytest rag/tests/` (нужен `pytest`, не входит в `requirements.txt` —
установить отдельно при необходимости: `pip install pytest`).

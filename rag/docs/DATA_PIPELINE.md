# Пайплайн данных: почта → корпус → Qdrant, и откуда берётся FAQ

Весь код пайплайна живёт в `data_pipeline/` (готовит данные) и `rag/scripts/`
(индексирует их в Qdrant). Полная справка по скриптам — `data_pipeline/README.md`;
здесь — описание пайплайна целиком, с картинкой потока данных и объяснением
"почему так".

## Общая схема

```
IMAP (почта менеджеров)
   │  data_pipeline/export_manager_mailboxes.py
   ▼
mailbox_export/                      — сырая выгрузка (threads/*.txt, all_messages.jsonl, index.csv)
   │  data_pipeline/clean_thread_quotes.py
   ▼
mailbox_export_clean/                — очищенные от вложенных цитат переписки
   │  data_pipeline/prepare_rag_corpus.py
   ▼
mailbox_export_RAG/corpus.jsonl      — корпус для индексации (~5935 chunks на паузе проекта)
   │
   │                     knowledge_base/clients_stats/Clients_stats_v4.csv (ручная разметка клиентов)
   │                          │
   │                          │  data_pipeline/build_faq_llm.py (или build_faq_catalog.py)
   │                          ▼
   │                     knowledge_base/v4/client_faq_review.csv  (~224 вопроса)
   │                          │
   ▼                          ▼
            rag/scripts/index_corpus.py
                     │  (mtr_rag/loaders.py → mtr_rag/embeddings.py → Qdrant upsert)
                     ▼
              Qdrant `movetorussia_kb`
                     │
        (опционально, отдельный поток)
   Yandex Disk /rag_corpus/*.txt ──► mtr_rag/yandex_disk_sync.py ──► Qdrant (source=yandex_disk)
```

Все данные (`mailbox_export*`, `knowledge_base/`) — **gitignored**, живут
только локально/в бэкапах. В git — только код, который их обрабатывает.

## Шаг 1 — Выгрузка почты (IMAP)

**Скрипт:** `data_pipeline/export_manager_mailboxes.py`

- Источник: почтовые ящики трёх менеджеров на Yandex (IMAP, read-only —
  `EXAMINE` + `BODY.PEEK[]`, письма НЕ помечаются прочитанными и не изменяются).
- Забирает **все исходящие** (Sent) и **все прочитанные входящие** (`\Seen` в
  INBOX) письма.
- Группирует письма по клиенту (внешний адрес, не `@arkvostok.com`,
  автоматически отфильтровываются служебные/рассылочные адреса).
- Нужны переменные `.env` (в корне репозитория): `E_NOVIK_MAIL_ADRESS/KEY`,
  `A_ANTONOVA_MAIL_ADRESS/KEY`, `N_PERRY_MAIL_ADRESS/KEY` — email и
  пароль приложения (не обычный пароль от почты!) для каждого ящика.
- Результат — `mailbox_export/`:
  - `threads/<client>.txt` — единая переписка по клиенту в человекочитаемом
    формате (`КЛИЕНТ: ...`, `ПИСЕМ: N`, разделители `===`, заголовки писем).
  - `all_messages.jsonl`, `index.csv`, `client_message_stats.csv`, `summary.json`.
- Устойчив к обрыву IMAP-сессии (Yandex рвёт FETCH после ~1500-2000 операций —
  скрипт сам переподключается).

## Шаг 2 — Очистка вложенных цитат

**Скрипт:** `data_pipeline/clean_thread_quotes.py`

Убирает вложенные цитаты предыдущих писем внутри тела письма (типичная
проблема "почтовых клиентов", которые вставляют всю переписку целиком в
каждый новый ответ). Результат — `mailbox_export_clean/`.

## Шаг 3 — Подготовка корпуса для RAG

**Скрипт:** `data_pipeline/prepare_rag_corpus.py`

Это ключевой шаг, который решает, "что такое один chunk":

1. Парсит `.txt`-треды (формат заголовков `[МЕНЕДЖЕР] (email)` / `[КЛИЕНТ]`,
   поля `Тема:`/`От:`/`Кому:`/`Дата:`).
2. Дочищает тело каждого письма:
   - вырезает висящие "хвосты" цитат (`schrieb`, `wrote`, `написал(а)` и т.п.
     на нескольких языках — эвристика по последнему абзацу письма);
   - вырезает повторяющуюся подпись менеджера (`-- Kind regards ... MovetoRussia.com ...`);
   - нормализует пробелы/переносы строк.
3. **Группирует письма в "exchanges"** — связка "вопрос(ы) клиента + ответ(ы)
   менеджера". Это и есть смысловая единица (chunk) для эмбеддинга, а не
   отдельное письмо: одно письмо клиента может быть слишком узким контекстом,
   а целый тред — слишком широким и разнородным.
4. Помечает малоинформативные exchanges флагом `low_signal` (< 12 слов и без
   "информативных" маркеров — цифры, визы, цены, даты, документы) — **не
   удаляет**, просто помечает, чтобы retrieval мог их опционально исключать.
5. Определяет язык exchange (`langdetect`, опционально).
6. **Опционально `--distill`**: прогоняет каждый exchange через DeepSeek,
   получая компактную "карточку знания" (ситуация клиента + суть ответа).
   Требует `DEEPSEEK_API_KEY`, стоит денег и времени — по умолчанию выключено.

Результат — `mailbox_export_RAG/`:
- `threads/<thread_id>.json` — полное дерево писем + exchanges по треду.
- `corpus.jsonl` — плоский список chunks, **это и есть вход для индексации**.
  Формат одной записи (см. также `rag/README.md` → "Формат данных"):

  | Поле | Тип | Описание |
  |---|---|---|
  | `id` | str | `{thread_id}__ex{NNN}` |
  | `thread_id` | str | id треда |
  | `client_email` | str | email клиента |
  | `manager_emails` | list[str] | email(-ы) менеджера(-ов) в exchange |
  | `subject` | str | тема письма |
  | `date_start` / `date_end` | str (ISO) | диапазон дат exchange |
  | `language` | str \| null | определённый язык |
  | `low_signal` | bool | малоинформативный exchange |
  | `word_count` | int | число слов |
  | `text` | str | очищенный текст (`КЛИЕНТ (дата): ...` / `МЕНЕДЖЕР (дата): ...`) |
  | `distilled` | str \| null | карточка знания от DeepSeek (если был `--distill`) |
  | `source` | str | всегда `"mailbox_thread"` |

- `prepare_report.txt` — отчёт: сколько тредов/писем/exchanges обработано,
  сколько `low_signal`, примеры вырезанных цитат/подписей (для проверки
  эвристик вручную).

## Шаг 4 — FAQ-каталог

**Вход:** `mailbox_export_clean/threads/*.txt` + вручную размеченный
`knowledge_base/clients_stats/Clients_stats_v4.csv` (заказчик отмечает, какие
клиенты релевантны для построения FAQ — этот файл **не генерируется
скриптами**, это ручная работа).

**Рекомендуемый скрипт (используется сейчас):** `data_pipeline/build_faq_llm.py`
— полностью через DeepSeek, три этапа:

1. **Extract (map)** — для каждого двустороннего диалога LLM извлекает пары
   "вопрос клиента → ответ менеджера" (только по-настоящему отвечающие пары,
   без шаблонных фраз/scheduling). Кэшируется в
   `knowledge_base/v4/_faq_intermediate/llm_pairs/`.
2. **Merge (reduce)** — LLM объединяет похожие вопросы, считает `frequency`,
   формирует канонический Q+A на английском. Промежуточный кэш —
   `llm_merge/`, финальный список — `llm_canonical.json`.
3. **Semantic merge** — дополнительная дедупликация "по смыслу" (например,
   "перевести накопления в Россию" и "перевести пенсию" — один и тот же
   интент) через тематические "корзины" (`INTENT_BUCKETS`) + LLM.

Результат — `knowledge_base/v4/`:
- `client_faq_review.csv` — **основной файл, который индексируется в Qdrant**
  (~224 вопроса на паузе проекта). Разделитель `;`, кодировка UTF-8 BOM.
  Колонки: `number;theme;theme_label;frequency;languages;question;question_original;answer;variants`.
- `client_faq_frequent.csv` — только вопросы с `frequency >= 2`.
- `client_faq.csv` — полный список без review-фильтра.
- `faq_llm_stats.json` — статистика сборки (сколько тредов/пар/канонических записей).

**Альтернатива (быстрее и дешевле, без LLM для extract):** `build_faq_catalog.py`
— эвристика + Jaccard-кластеризация вместо LLM. Флаг `--force` для пересборки.

Скрипт можно запускать по частям (`--max-threads N` для теста, `--merge-only`,
`--semantic-only`, `--force`/`--force-merge`/`--force-semantic` для
пересборки конкретного этапа) — все промежуточные результаты кэшируются в
`_faq_intermediate/`, повторный запуск без флагов не тратит новые вызовы DeepSeek.

## Шаг 5 — Индексация в Qdrant

**Скрипт:** `rag/scripts/index_corpus.py` (подробности — [`QDRANT.md`](QDRANT.md)
и [`ARCHITECTURE.md`](ARCHITECTURE.md)). Загружает `corpus.jsonl` +
`client_faq_review.csv` через `mtr_rag/loaders.py`, эмбеддит через Voyage
(`mtr_rag/embeddings.py`) и загружает в Qdrant (`mtr_rag/qdrant_store.py`).

```powershell
cd rag
python scripts\index_corpus.py --dry-run       # оценка токенов/стоимости, без записи
python scripts\index_corpus.py --recreate      # первая полная индексация
python scripts\index_corpus.py                 # инкрементальный re-run (upsert по id chunk'а)
```

## Опциональный слой: Yandex Disk ("официальные файлы")

Отдельный, независимый источник данных — `.txt`-файлы, вручную загруженные
на Yandex Disk в папку `/rag_corpus`. Считаются самым авторитетным источником
(`priority=highest` в промпте, см. `mtr_rag/mail_writing_prompt.py`) —
перекрывают FAQ и переписку при конфликте фактов.

- **Код:** `mtr_rag/yandex_disk_sync.py` — скачивает `.txt` файлы, строит
  локальное зеркало `rag/data/yandex_disk/files/`, пересобирает
  `rag/data/yandex_disk/corpus.jsonl` + `manifest.json` (для diff added/changed/removed).
- **Запуск:** `rag/scripts/update_yandex_disk_corpus.py` — по расписанию
  через systemd timer на **проде** (`movetorussia-rag-disk-sync.timer`,
  ежедневно в 00:00 МСК, см. `rag/deploy/movetorussia-rag-disk-sync.*`).
- **Настройка с нуля:** см. раздел "Yandex Disk supplemental corpus" в
  `rag/deploy/README.md` — нужен OAuth-токен (`scripts/get_yandex_disk_token.py`,
  требует `YANDEX_OAUTH_CLIENT_ID/SECRET`, зарегистрированные на https://oauth.yandex.ru).
- Индексируется отдельно от `index_corpus.py`, не смешивается с
  mailbox/FAQ-чанками — retriever резервирует под него отдельные "слоты"
  (`MTR_DISK_RESERVE_SLOTS`, `MTR_DISK_MIN_SCORE`, см. `mtr_rag/retriever.py`).

Это опциональный слой: без него бот полностью работоспособен на корпусе почты
+ FAQ, просто без "самого авторитетного" источника фактов.

## Что можно пересобрать, а что — беречь

- **Готовые данные (`corpus.jsonl`, `client_faq_review.csv`) не перегенерировать
  без явной необходимости** — пересборка с нуля требует нового
  IMAP-экспорта, повторных вызовов DeepSeek (расходы, см. [`COSTS.md`](COSTS.md))
  и может изменить состав/формулировки корпуса.
- Если появилась новая переписка — можно **дозагрузить** только новые данные:
  повторный запуск `export_manager_mailboxes.py` (например, с `--since`) →
  `clean_thread_quotes.py` → `prepare_rag_corpus.py` перезапишет `corpus.jsonl`
  целиком, а `index_corpus.py` (без `--recreate`) сделает upsert по id —
  старые chunks не дублируются.
- `knowledge_base/clients_stats/Clients_stats_v4.csv` — это **ручная работа
  заказчика**, автоматически не создаётся; без него `build_faq_llm.py`
  просто не будет знать, каких клиентов включать в scope.

# Qdrant: что в базе и как её пересобрать/восстановить

## Где живёт

Self-hosted Qdrant в Docker на VPS (`207.244.254.188`), один общий инстанс
для **обоих** ботов — prod и dev (`rag/docker-compose.yml`):

```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.13.2
    ports:
      - "127.0.0.1:6333:6333"   # HTTP API
      - "127.0.0.1:6334:6334"   # gRPC
    volumes:
      - qdrant_data:/qdrant/storage
```

Порты забинжены только на `127.0.0.1` — Qdrant не торчит наружу, доступен
только с самого VPS (боты и скрипты обращаются по `localhost:6333`).
Данные — в Docker volume `qdrant_data` (персистентны между перезапусками
контейнера, **но не защищены от удаления volume**).

## Коллекция `movetorussia_kb`

Единственная "боевая" коллекция (имя настраивается `QDRANT_COLLECTION`,
дефолт — `movetorussia_kb`). Создаётся/пересобирается через
`mtr_rag/qdrant_store.py::ensure_collection()`, вызывается из
`scripts/index_corpus.py`.

- **Векторы:** размер = `VOYAGE_OUTPUT_DIMENSION` (по умолчанию `1024`),
  метрика — `COSINE`.
- **Payload-индексы** (для быстрой фильтрации): `source` (keyword),
  `thread_id` (keyword), `client_email` (keyword), `manager_emails` (keyword),
  `low_signal` (bool), `date_start` (datetime), `language` (keyword).
- **Point id:** детерминированный `uuid5` от строкового `id` chunk'а
  (`chunk_id_to_point_id()`, фиксированный namespace UUID в коде) — повторная
  индексация того же chunk **перезаписывает** точку, а не создаёт дубликат.
  Поэтому `index_corpus.py` без `--recreate` можно безопасно гонять
  инкрементально сколько угодно раз.

### Что лежит в payload одной точки

Полная модель — `Chunk` (`mtr_rag/schema.py`), в Qdrant хранится как
`to_payload()`: `id`, `thread_id`, `source`, `text`, `subject`,
`client_email`, `manager_emails`, `date_start`, `date_end`, `language`,
`low_signal`, `word_count`, `distilled`, плюс специфичные для источника поля
из `extra` (`theme`/`frequency`/`question`/`answer` для FAQ;
`file_path`/`content_sha256`/`priority` для Yandex Disk).

### Три значения `source`

| `source` | Кол-во на момент паузы | Откуда | Приоритет в промпте |
|---|---|---|---|
| `mailbox_thread` | ~5935 chunks | `mailbox_export_RAG/corpus.jsonl` | обычный (самый низкий) |
| `faq_catalog` | ~224 | `knowledge_base/v4/client_faq_review.csv` | средний |
| `yandex_disk` | переменное, зависит от файлов на Disk | `/rag_corpus` на Yandex Disk | `highest` — перекрывает остальные при конфликте |

## Полная пересборка коллекции с нуля

Требуется: доступный Qdrant (локально или на VPS), рабочий `VOYAGE_API_KEY`,
данные на месте (`corpus.jsonl` + `client_faq_review.csv`, см.
[`DATA_PIPELINE.md`](DATA_PIPELINE.md)).

```powershell
cd rag
python scripts\index_corpus.py --dry-run      # проверить объём/стоимость перед реальным прогоном
python scripts\index_corpus.py --recreate     # удаляет старую коллекцию и пересоздаёт с нуля
```

Полная пересборка (~6000 mailbox chunks + ~225 FAQ) занимает от нескольких
минут до получаса — упирается в лимит запросов Voyage API (см.
`VOYAGE_BATCH_SIZE`, флаг `--sleep-between-batches`, по умолчанию 2 сек между
батчами). Опционально `--no-faq` — только mailbox-корпус, без FAQ.

Слой Yandex Disk индексируется отдельно и не трогается `index_corpus.py` —
после пересборки основной коллекции нужно отдельно прогнать
`scripts/update_yandex_disk_corpus.py`, если этот слой используется (см.
[`DATA_PIPELINE.md`](DATA_PIPELINE.md#опциональный-слой-yandex-disk-официальные-файлы)).

## Резервное копирование / восстановление

**На данный момент в проекте нет отдельного backup-скрипта для Qdrant** —
единственная защита данных — это то, что коллекцию можно **пересобрать с
нуля** из исходных файлов (`corpus.jsonl` + FAQ CSV), которые сами по себе
надо беречь (см. предупреждение в `data_pipeline/README.md`). Поэтому:

- **Самый надёжный способ "backup"** — хранить `mailbox_export_RAG/corpus.jsonl`
  и `knowledge_base/v4/client_faq_review.csv` (и, если используется,
  `rag/data/yandex_disk/`) в бэкапе (см. `ACCESS_CHECKLIST.md` про Yandex Disk) —
  восстановление базы = `index_corpus.py --recreate` из этих файлов.
- **Если нужен снапшот самой Qdrant-коллекции** (без повторного эмбеддинга,
  быстрее и без затрат на Voyage) — Qdrant поддерживает встроенные snapshot'ы
  (`POST /collections/{name}/snapshots`), но в этом проекте это не
  автоматизировано — нужно настраивать отдельно при необходимости
  (см. [официальную документацию Qdrant](https://qdrant.tech/documentation/concepts/snapshots/)).
- **Клонирование коллекции между инстансами Qdrant** (например, для
  экспериментов или переноса на новый сервер без потери векторов, без
  повторного вызова Voyage) — готовый скрипт:

  ```powershell
  python scripts\clone_qdrant_collection.py `
      --source-url http://localhost:6333 --source-collection movetorussia_kb `
      --target-url http://<новый-хост>:6333 --target-collection movetorussia_kb `
      --recreate-target
  ```

  Копирует read-only со старого Qdrant (никогда не пишет в источник, при
  просадке статуса коллекции — прерывается).

- **Донести недостающие точки** из одной коллекции в другую (без полной
  пересборки) — `scripts/sync_missing_qdrant_points.py` (опционально с
  фильтром `--source-filter <source>`, например только `yandex_disk`).

## Перенос на новый VPS / новый Qdrant

1. Поднять Qdrant на новом сервере (тот же `docker-compose.yml`).
2. Либо: `clone_qdrant_collection.py` со старого на новый (если старый ещё
   жив) — быстрее, без затрат на Voyage.
3. Либо (если старого Qdrant уже нет): `index_corpus.py --recreate` с нуля
   из `corpus.jsonl` + FAQ CSV — дольше, требует Voyage API вызовов (см.
   [`COSTS.md`](COSTS.md) про порядок стоимости).
4. Обновить `QDRANT_URL` в `rag/.env` на обоих инстансах (prod/dev), если
   Qdrant переехал на другой адрес/порт.

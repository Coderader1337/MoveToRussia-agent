# RAG-ассистент MoveToRussia (n8n + Qdrant + DeepSeek + Telegram)

Внутренний Q&A для менеджеров на основе **очищенных переписок** `mailbox_export_clean`, FAQ и загружаемых `.txt` файлов через Telegram.

## Архитектура

```
mailbox_export_clean/threads/*.txt
        │
        ▼
scripts/rag_index_qdrant.py  ──embed──►  embedder (multilingual-e5)
        │                                      │
        └──────────────►  Qdrant  ◄────────────┘
                              ▲
Telegram (.txt upload) ──n8n──┘
Telegram (вопрос) ──n8n──► embed → search → DeepSeek → ответ
```

| Компонент | Назначение |
|-----------|------------|
| **Qdrant** | Векторное хранилище (`movetorussia_kb`) |
| **Embedder** | OpenAI-compatible API, модель `intfloat/multilingual-e5-small` |
| **DeepSeek** | Генерация ответа с контекстом (RAG) |
| **n8n** | Telegram-бот: вопросы + загрузка `.txt` |
| **mailbox_export_clean** | Источник переписок (1439 тредов) |

## 1. Запуск инфраструктуры

```powershell
cd rag
copy .env.example .env
# При необходимости отредактируйте порты

docker compose up -d --build
docker compose ps
curl http://localhost:6333/healthz
curl http://localhost:8081/health
```

Первый запуск embedder скачивает модель (~100 MB) — подождите 1–2 минуты.

## 2. Первичная индексация переписок

Из корня репозитория (нужен запущенный Qdrant + embedder):

```powershell
# Проверка без записи
python scripts/rag_index_qdrant.py --dry-run

# Полная индексация mailbox_export_clean
python scripts/rag_index_qdrant.py --recreate

# + FAQ из knowledge_base/v4 (рекомендуется)
python scripts/rag_index_qdrant.py --recreate --include-faq
```

Ожидаемый объём: ~15 000 чанков (письма) + ~300 FAQ.

Переменные (`.env` в корне или `rag/.env`):

```env
QDRANT_URL=http://localhost:6333
EMBEDDER_URL=http://localhost:8081
RAG_COLLECTION=movetorussia_kb
```

## 3. Настройка n8n

### 3.1 Импорт workflow

1. n8n → **Workflows** → **Import from File**
2. Файл: `n8n/rag_telegram_assistant.json`
3. Активируйте workflow

### 3.2 Credentials

| Credential | Тип | Поля |
|------------|-----|------|
| Telegram | Telegram API | Bot Token от @BotFather |

### 3.3 Environment variables (n8n)

| Переменная | Пример | Описание |
|------------|--------|----------|
| `DEEPSEEK_API_KEY` | `sk-...` | API-ключ DeepSeek |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Модель |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Base URL |
| `QDRANT_URL` | `http://qdrant:6333` | URL Qdrant (из n8n) |
| `EMBEDDER_URL` | `http://embedder:8081` | URL embedder |
| `RAG_COLLECTION` | `movetorussia_kb` | Имя коллекции |
| `RAG_TOP_K` | `6` | Число фрагментов контекста |
| `TELEGRAM_ALLOWED_CHAT_IDS` | `123456,789012` | Опционально: whitelist chat_id |

**n8n Cloud + VPS:** Qdrant и embedder должны быть доступны по URL с n8n (публичный IP VPS или tunnel).  
**n8n self-hosted на том же VPS:** используйте имена сервисов Docker-сети (`http://qdrant:6333`).

### 3.4 Docker-сеть (n8n + RAG на одном VPS)

```yaml
# Подключите n8n к сети rag_rag-network или запустите всё в одном compose
networks:
  rag-network:
    external: true
    name: rag_rag-network
```

## 4. Использование Telegram-бота

| Действие | Как |
|----------|-----|
| Справка | `/start` или `/help` |
| Вопрос | Любой текст |
| Статистика | `/stats` |
| Пополнение БЗ | Отправить `.txt` файл |

Формат загрузки:
- Произвольный текст (абзацы)
- Файлы в формате `mailbox_export_clean/threads/*.txt` — парсятся как переписки

## 5. RAG-пайплайн (канон)

1. **Chunking** — одно письмо = один чанк (+ FAQ, + upload chunks)
2. **Embedding** — `passage:` / `query:` префиксы E5
3. **Vector store** — Qdrant, cosine similarity
4. **Retrieval** — top-K (по умолчанию 6)
5. **Augmentation** — контекст в промпт
6. **Generation** — DeepSeek, temperature 0.2
7. **Guardrails** — «только из контекста», без галлюцинаций

## 6. Обновление базы после нового экспорта

```powershell
# 1. Экспорт и очистка (существующие скрипты)
python scripts/export_manager_mailboxes.py
python scripts/clean_thread_quotes.py

# 2. Переиндексация
python scripts/rag_index_qdrant.py --recreate --include-faq
```

## 7. Troubleshooting

| Проблема | Решение |
|----------|---------|
| `Connection refused` к Qdrant | `docker compose ps` в `rag/` |
| Embedder долго стартует | Подождите healthcheck; смотрите `docker compose logs embedder` |
| Пустые ответы | Проверьте `--dry-run` — есть ли чанки; выполнена ли индексация |
| n8n не достучится до localhost | На VPS используйте IP/hostname сервиса, не `localhost` |
| Telegram «Доступ запрещён» | Очистите `TELEGRAM_ALLOWED_CHAT_IDS` или добавьте свой chat_id |

## Файлы

| Путь | Назначение |
|------|------------|
| `rag/docker-compose.yml` | Qdrant + embedder |
| `scripts/rag_chunk.py` | Chunking из `mailbox_export_clean` |
| `scripts/rag_index_qdrant.py` | Bulk-индексация |
| `n8n/rag_telegram_assistant.json` | Telegram RAG workflow |
| `prompts/rag_assistant_prompt.py` | Системный промпт (справочно) |

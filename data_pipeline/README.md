# Data pipeline — подготовка корпуса и FAQ для RAG

Скрипты этой папки готовят данные, которые потребляет `rag/` (см. `rag/mtr_rag/config.py`:
`MTR_CORPUS_PATH` и `MTR_FAQ_CSV_PATH` по умолчанию смотрят на `mailbox_export_RAG/corpus.jsonl`
и `knowledge_base/v4/client_faq_review.csv` в корне репозитория).

Все скрипты запускаются **из корня репозитория**:

```powershell
cd C:\Users\alpal\.cursor\mail_agent
python data_pipeline\export_manager_mailboxes.py
```

Зависимости: `pip install -r data_pipeline/requirements.txt` (плюс переменные окружения в корневом `.env`).

## Полный цикл пересборки (через полгода и позже)

```powershell
# 1. Выгрузка почты по IMAP (read-only) → mailbox_export/
#    Нужны в .env: E_NOVIK_MAIL_ADRESS/KEY, A_ANTONOVA_MAIL_ADRESS/KEY, N_PERRY_MAIL_ADRESS/KEY
python data_pipeline\export_manager_mailboxes.py

# 2. Очистка вложенных цитат → mailbox_export_clean/
python data_pipeline\clean_thread_quotes.py

# 3. Сборка корпуса для RAG (парсинг, группировка в exchanges, low_signal) → mailbox_export_RAG/corpus.jsonl
python data_pipeline\prepare_rag_corpus.py
#   Опционально: --distill (нужен DEEPSEEK_API_KEY) — компактные "карточки знания" на каждый exchange

# 4. FAQ-каталог (нужен knowledge_base/clients_stats/Clients_stats_v4.csv — размечается заказчиком вручную)
#    Рекомендуемый вариант — через LLM:
python data_pipeline\build_faq_llm.py
#    Альтернатива (эвристика без LLM, быстрее и дешевле):
python data_pipeline\build_faq_catalog.py --force
# Результат: knowledge_base/v4/client_faq_review.csv (+ frequent/quality_once/stats)

# 5. Индексация в Qdrant (уже в rag/, требует запущенный Qdrant + ключи Voyage/DeepSeek)
cd rag
python scripts\index_corpus.py --recreate
python scripts\smoke_test.py
```

## Файлы

| Файл | Роль |
|---|---|
| `mail_imap_utils.py` | Общие IMAP-хелперы (используется `export_manager_mailboxes.py`) |
| `export_manager_mailboxes.py` | Шаг 1: read-only IMAP-выгрузка → `mailbox_export/` |
| `export_client_message_stats_csv.py` | Пересобрать `mailbox_export/client_message_stats.csv` из `index.csv` без повторного IMAP |
| `clean_thread_quotes.py` | Шаг 2: чистка вложенных цитат → `mailbox_export_clean/` |
| `prepare_rag_corpus.py` | Шаг 3: чистка + группировка в exchanges → `mailbox_export_RAG/corpus.jsonl` |
| `build_knowledge_base.py`, `build_kb_versions.py` | Общие функции для сборки knowledge base (загрузка писем, DeepSeek map/reduce); переиспользуются `build_faq_*.py`. Собственный CLI (`build_kb_versions.py`) собирает старые версии `knowledge_base/v1-v4/movetorussia_agent_kb.md` — не нужен для текущего RAG, оставлен как зависимость |
| `build_faq_catalog.py` | Шаг 4 (эвристика): вопросы клиентов → `knowledge_base/v4/client_faq*.csv` |
| `build_faq_llm.py` | Шаг 4 (LLM, рекомендуется): то же через DeepSeek map/reduce |

## Данные (не в git, только локально)

- `mailbox_export/` — сырая IMAP-выгрузка
- `mailbox_export_clean/` — очищенные переписки
- `mailbox_export_RAG/` — корпус для индексации (`corpus.jsonl` + per-thread JSON)
- `knowledge_base/clients_stats/Clients_stats_v4.csv` — ручная разметка клиентов заказчиком (вход для FAQ-пайплайна, не генерируется скриптами)
- `knowledge_base/v4/` — выход FAQ-пайплайна

Существующие данные (~290 МБ) **не перегенерировать без явного запроса** — пересборка с нуля требует нового
IMAP-экспорта и повторных DeepSeek-вызовов (расходы + время).

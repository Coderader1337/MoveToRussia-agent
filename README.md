# MovetoRussia — RAG-ассистент (проект на паузе)

Репозиторий разделён на три независимые части. Основной, активно поддерживаемый
продукт — **RAG-ассистент** (`rag/`). Остальное — данные и legacy-код, которые ему
не мешают, но нужны для полного цикла (пересборка корпуса, деплой).

| Папка | Что это | Деплоится? |
|---|---|---|
| [`rag/`](rag/README.md) | RAG-ассистент для Telegram (LangChain, Voyage, DeepSeek, Qdrant, aiogram) — основной продукт | Да, целиком на VPS (см. `rag/deploy/README.md`) |
| [`data_pipeline/`](data_pipeline/README.md) | Скрипты подготовки данных для RAG: IMAP-выгрузка → очистка → корпус → FAQ-каталог | Нет, запускается локально/вручную |
| [`mail_agent/`](mail_agent/README.md) | Legacy Mail copilot (n8n + Google Sheets + Docker API) — предшественник RAG, изолирован, не используется RAG | Нет |
| `mailbox_export/`, `mailbox_export_clean/`, `mailbox_export_RAG/`, `knowledge_base/` | Данные (gitignored, только локально) — вход/выход `data_pipeline/`, источник для `rag/` | — |

`rag/` и `mail_agent/` полностью независимы друг от друга: `rag/` не импортирует и не
читает ничего из `mail_agent/`, `data_pipeline/` — общий поставщик данных только для `rag/`.

## Если открываете репозиторий после паузы (см. `.cursor/rules/mail-agent.mdc` для деталей)

1. **Актуализировать данные** (по желанию — готовый корпус уже лежит в `mailbox_export_RAG/`
   и `knowledge_base/v4/`, пересборка нужна только если появилась новая переписка):
   см. `data_pipeline/README.md` — полный цикл IMAP → корпус → FAQ.
2. **Поднять и проверить RAG локально**: `rag/README.md` — установка зависимостей,
   `python scripts/index_corpus.py`, `python scripts/smoke_test.py`.
3. **Задеплоить**: `rag/deploy/README.md` — CI/CD через GitHub Actions
   (push в `rag_develop` → dev-бот, PR в `master` → prod-бот), либо вручную по SSH.
4. Legacy-автоматизацию (`mail_agent/`) трогать только по явному запросу — см. `mail_agent/README.md`.
5. **Полная документация по воспроизведению проекта** (пошаговая инструкция с
   нуля, пайплайн данных, архитектура кода, Qdrant, CI/CD, поддержка,
   стоимость, список ключей/доступов) — [`rag/docs/`](rag/docs/README.md).

## Конфигурация

- `.env` в корне репозитория — общие секреты, которые читает и `rag/` (как fallback,
  приоритет у `rag/.env`), и легаси-скрипты (`data_pipeline/`, `mail_agent/`): IMAP-пароли
  почтовых ящиков менеджеров, `DEEPSEEK_API_KEY`, `VPS_IP`/`VPS_PASS` и т.д.
- `rag/.env` — специфичные для RAG ключи и настройки (Voyage, Qdrant, Telegram-бот).
- `mail_agent/.env`, `mail_agent/docker_api/.env` — легаси-конфиги (Google Sheets, Docker API).

Секреты и приватные ключи — только в `.env` / `~/.ssh/`, никогда в коде или правилах.

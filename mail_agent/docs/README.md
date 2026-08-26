# Документация Mail copilot (legacy, на паузе)

> Основной продукт компании — RAG-ассистент, документация по нему в
> [`../../rag/docs/`](../../rag/docs/README.md). Здесь — только legacy Mail
> copilot (n8n + Google Sheets + Docker API), см. `.cursor/rules/mail-agent.mdc`.

**С чего начать:**

1. [`STATUS.md`](STATUS.md) — текущий статус: что готово и было проверено, что
   не протестировано, известные ограничения.
2. [`SETUP.md`](SETUP.md) — как поднять проект с нуля: зависимости, доступы,
   `.env`, Docker API, n8n workflow.
3. [`API.md`](API.md) — Mail API (Docker API): эндпоинты, конфигурация,
   реализация, ограничения.
4. [`N8N_WORKFLOW.md`](N8N_WORKFLOW.md) — разбор нод `Mail Agent.json` и
   вспомогательного webhook-примера `docker_api/n8n_workflow_api_integration.json`.
5. [`SCRIPTS.md`](SCRIPTS.md) — какой скрипт что делает, как запускать,
   какие переменные `.env` нужны.
6. [`ACCESS_CHECKLIST.md`](ACCESS_CHECKLIST.md) — полный список ключей/доступов
   проекта (без значений) + чек-лист независимого доступа к аккаунтам.

**Диаграммы** (сгенерированы из `Mail Agent.json`):
[`architecture_diagram.md`](architecture_diagram.md) (компоненты и роли),
[`logical_flow_diagram.md`](logical_flow_diagram.md) (шаг за шагом, ветвления
ошибок).

**Важно:** здесь нет ни одного реального секрета — только имена переменных,
названия сервисов и описания процессов. Реальные ключи — в `.env`-файлах (не в
git) и в личном хранилище паролей владельца.

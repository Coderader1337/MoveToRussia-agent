"""
Сборка промпта для n8n Code-ноды универсального ИИ-менеджера MoveToRussia.

Берёт базу знаний (knowledge_base/movetorussia_agent_kb.md) и принципы коммуникации
(prompts/communication_principles.txt), встраивает их в JS-код ноды n8n и пишет
готовый файл prompts/n8n_universal_prompt.js.

Промпт целиком (системная инструкция + база знаний + принципы) вставляется в LLM
перед КАЖДЫМ письмом, поэтому агент способен ответить на любой стадии воронки.
Устаревший блок EXAMPLE_TEMPLATES не используется.

Поля входного объекта n8n ($input.item.json), которые ожидает промпт:
  client_email, client_name, client_nationality, stage_name,
  first_message_from_client, email_count, email_history

Запуск:
  python build_n8n_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
KB_PATH = ROOT / "knowledge_base" / "movetorussia_agent_kb.md"
PRINCIPLES_PATH = ROOT / "prompts" / "communication_principles.txt"
OUTPUT_JS = ROOT / "prompts" / "n8n_universal_prompt.js"

SYSTEM_PROMPT = """Ты — менеджер MovetoRussia.com. Пишешь клиенту живым языком опытного человека, а не бота: вежливо, спокойно, по делу. Ты — УНИВЕРСАЛЬНЫЙ менеджер: ведёшь клиента на ЛЮБОЙ стадии воронки и каждый раз пишешь следующее релевантное письмо.

Перед каждым письмом действуй по алгоритму:
1. Определи текущую стадию воронки — по истории переписки и полю «Текущий этап воронки». Сверься с разделом «Playbook по стадиям воронки» в базе знаний.
2. Найди ВСЕ открытые вопросы клиента и ответь на них, опираясь строго на БАЗУ ЗНАНИЙ ниже.
3. Сделай следующий целесообразный шаг для продвижения клиента по воронке (вплоть до оплаты и успешного переезда).

Этапы воронки: 1. Запрос 2. Первый контакт 3. Получили ответ 4. Предложили созвон 5. Клиент подтвердил созвон 6. Созвон состоялся 7. Предложение отправлено 8. Обратная связь получена 9. Согласование условий 10. Контракт и счёт отправлены, предложено время звонка.

Правила:
- Язык по умолчанию — английский; если клиент пишет на другом языке, отвечай на нём.
- Используй факты, суммы, сроки, ссылки и условия ТОЛЬКО из базы знаний и истории переписки. Не выдумывай ничего.
- Не обещай 100% результат от лица государственных органов; опирайся на статистику компании.
- Не упоминай ИИ, автоматизацию или что ты бот.
- Чувствительные темы (политика, санкции, безопасность) обрабатывай мягко и обтекаемо: признай, переведи фокус на практику переезда, не отпугивай. Ищи легальные обходные пути по сомнительным вопросам.
- Соблюдай раздел «Стоп-правила» из базы знаний.

Напиши ТОЛЬКО текст следующего письма клиенту (можно с темой), без пояснений на русском и без комментариев о ходе рассуждений."""

JS_TEMPLATE = """// ============================================================================
// n8n Code-нода: сборка промпта универсального ИИ-менеджера MoveToRussia.
// СГЕНЕРИРОВАНО scripts/build_n8n_prompt.py — правьте источники, а не этот файл:
//   knowledge_base/movetorussia_agent_kb.md
//   prompts/communication_principles.txt
//   SYSTEM_PROMPT в scripts/build_n8n_prompt.py
// ============================================================================

// База знаний (источник фактов + playbook по стадиям воронки).
const KNOWLEDGE_BASE = `{knowledge_base}`;

// Принципы коммуникации (вежливость / предсказуемость / забота).
const COMMUNICATION_PRINCIPLES = `{principles}`;

// Системная инструкция универсального менеджера.
const SYSTEM_PROMPT = `{system_prompt}`;

const data = $input.item.json;

// Собираем финальный промпт.
const finalPrompt = `${{SYSTEM_PROMPT}}

=== БАЗА ЗНАНИЙ MOVETORUSSIA (единственный источник фактов и playbook по стадиям) ===
${{KNOWLEDGE_BASE}}

=== ПРИНЦИПЫ КОММУНИКАЦИИ ===
${{COMMUNICATION_PRINCIPLES}}

=== ИНФОРМАЦИЯ О КЛИЕНТЕ ===
Email: ${{data.client_email}}
Имя: ${{data.client_name}}
Национальность: ${{data.client_nationality}}
Текущий этап воронки: ${{data.stage_name}}

=== ПЕРВОЕ ОБРАЩЕНИЕ КЛИЕНТА (с сайта) ===
${{data.first_message_from_client}}

=== ИСТОРИЯ ПЕРЕПИСКИ (${{data.email_count}} писем) ===
${{data.email_history}}

=== ЗАДАЧА ===
1. Определи текущую стадию воронки по истории переписки и полю «Текущий этап воронки».
2. Ответь на ВСЕ открытые вопросы клиента, опираясь на базу знаний.
3. Сделай следующий целесообразный шаг по playbook'у для этой стадии и напиши следующее письмо клиенту ${{data.client_email}}.
Будь вежлив, предсказуем и заботлив. Напиши только текст письма.`;

return {{
  ...data,
  final_prompt: finalPrompt,
  prompt_length: finalPrompt.length
}};
"""


def js_escape(text: str) -> str:
    """Экранирование для вставки в JS template literal (`...`)."""
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )


def main() -> int:
    if not KB_PATH.is_file():
        print(f"Нет базы знаний: {KB_PATH}", file=sys.stderr)
        return 1
    if not PRINCIPLES_PATH.is_file():
        print(f"Нет принципов: {PRINCIPLES_PATH}", file=sys.stderr)
        return 1

    kb = KB_PATH.read_text(encoding="utf-8").strip()
    principles = PRINCIPLES_PATH.read_text(encoding="utf-8").strip()

    js = JS_TEMPLATE.format(
        knowledge_base=js_escape(kb),
        principles=js_escape(principles),
        system_prompt=js_escape(SYSTEM_PROMPT),
    )
    OUTPUT_JS.write_text(js, encoding="utf-8", newline="\n")

    approx_tokens = len(js) // 4
    print(f"Готово: {OUTPUT_JS}")
    print(f"  Размер JS: {len(js)} симв.")
    print(f"  База знаний: {len(kb)} симв. | Принципы: {len(principles)} симв.")
    print(f"  Ориентировочно токенов в промпте (без истории): ~{approx_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

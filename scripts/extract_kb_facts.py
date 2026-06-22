"""
Извлечь из movetorussia_agent_kb.md два отдельных документа (старый файл не меняется):

  movetorussia_kb.md      — чистая база знаний (4 раздела: воронка, FAQ, факты, пояса)
  n8n_agent_prompt.md     — инструкции для ИИ-агента (роль, стиль, стоп-правила и т.д.)

Пример:
  python extract_kb_facts.py
  python extract_kb_facts.py --only 1 2
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
KB_ROOT = ROOT / "knowledge_base"

SECTION_HEADER = re.compile(r"^## (\d+)\.\s", re.M)
APPENDIX_MARKER = "## Приложение"


def split_numbered_sections(text: str) -> dict[int, str]:
    """Разбить текст на секции ## N. … до приложения или конца LLM-слоя."""
    body = text
    appendix_pos = body.find(APPENDIX_MARKER)
    if appendix_pos != -1:
        body = body[:appendix_pos]

    matches = list(SECTION_HEADER.finditer(body))
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[num] = body[start:end].strip()
    return sections


def _clean_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def transform_playbook(content: str) -> str:
    content = re.sub(
        r"^## 2\. Playbook по стадиям воронки",
        "## 1. Стадии воронки",
        content,
        count=1,
        flags=re.M,
    )
    content = re.sub(r"^### 2\.(\d+)", r"### 1.\1", content, flags=re.M)
    content = re.sub(
        r"\*\*Цель письма:\*\*\s*\n.*?(?=\n\*\*|\n---|\n### |\Z)",
        "",
        content,
        flags=re.S,
    )
    content = re.sub(
        r"\*\*Что включить в письмо:\*\*\s*\n.*?(?=\n\*\*Типичные формулировки|\n---|\n### |\Z)",
        "",
        content,
        flags=re.S,
    )
    content = re.sub(
        r"\*\*Типичные формулировки:\*\*",
        "**Типичные формулировки менеджеров:**",
        content,
    )
    return _clean_blank_lines(content)


def transform_faq(content: str) -> str:
    content = re.sub(
        r"^## 3\. FAQ[^\n]*",
        "## 2. FAQ: вопросы клиентов и эталонные ответы",
        content,
        count=1,
        flags=re.M,
    )
    content = re.sub(r"^### 3\.(\d+)", r"### 2.\1", content, flags=re.M)
    return content.strip()


def transform_facts(content: str) -> str:
    content = re.sub(
        r"^## 4\.[^\n]*",
        "## 3. Факты о процессе переезда",
        content,
        count=1,
        flags=re.M,
    )
    content = re.sub(r"^### 4\.(\d+)", r"### 3.\1", content, flags=re.M)
    content = content.replace(
        "Никогда не указывайте «переезд в Россию» как цель платежа.",
        "В назначении платежа не следует указывать «переезд в Россию» как цель.",
    )
    return content.strip()


def transform_timezone(content: str) -> str:
    content = re.sub(
        r"^## 5\.[^\n]*",
        "## 4. Часовые пояса и бронирование звонков",
        content,
        count=1,
        flags=re.M,
    )
    content = re.sub(r"^### 5\.(\d+)", r"### 4.\1", content, flags=re.M)
    content = content.replace(
        "### 4.1 Основной алгоритм",
        "### 4.1 Типичный порядок назначения звонка",
    )
    content = content.replace(
        "### 4.3 Что нельзя делать",
        "### 4.3 Типичные ошибки при scheduling",
    )
    replacements = {
        "**Запросить часовой пояс клиента**": "**Уточнение часового пояса клиента**",
        "**Предложить 2–3 конкретных временных окна**": "**Предложение 2–3 конкретных временных окон**",
        "**Уточнить предпочтительный канал:**": "**Выбор канала связи:**",
        "**Зафиксировать итог:**": "**Подтверждение:**",
        "**Напомнить:**": "**Напоминание перед звонком:**",
        "Всегда указывай и время клиента, и MSK.": "В переписке обычно указывают и время клиента, и MSK.",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    return content.strip()


def build_kb_md(sections: dict[int, str]) -> str:
    parts: list[str] = []
    if 2 in sections:
        parts.append(transform_playbook(sections[2]))
    if 3 in sections:
        parts.append(transform_faq(sections[3]))
    if 4 in sections:
        parts.append(transform_facts(sections[4]))
    if 5 in sections:
        parts.append(transform_timezone(sections[5]))
    return _neutralize_agent_refs("\n\n---\n\n".join(parts))


def _neutralize_agent_refs(text: str) -> str:
    """Убрать императивы про агента из фактологического слоя воронки."""
    replacements = {
        "Агент должен инициировать назначение точного времени.":
            "На этом этапе обычно назначают точное время звонка.",
        "Действия агента — непосредственно перед звонком и после него.":
            "Этап непосредственно перед звонком и сразу после него.",
        "Агент проверит ваш индивидуальный пакет после подписания договора.":
            "Индивидуальный пакет документов проверяется после подписания договора.",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def build_agent_prompt_md(sections: dict[int, str]) -> str:
    parts: list[str] = []
    if 1 in sections:
        parts.append(sections[1].rstrip("-\n "))
    for n in (6, 7, 8):
        if n in sections:
            chunk = sections[n]
            chunk = re.sub(
                r"\nНастоящая инструкция является.*",
                "",
                chunk,
                flags=re.S,
            ).strip().rstrip("-\n ")
            parts.append(chunk)
    return "\n\n---\n\n".join(parts)


def extract_dir(kb_dir: Path, *, label: str | None = None) -> tuple[Path, Path]:
    source = kb_dir / "movetorussia_agent_kb.md"
    if not source.is_file():
        raise FileNotFoundError(f"Нет {source}")

    tag = label or kb_dir.name
    text = source.read_text(encoding="utf-8")
    sections = split_numbered_sections(text)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    kb_path = kb_dir / "movetorussia_kb.md"
    kb_body = build_kb_md(sections)
    kb_header = (
        f"<!-- Извлечено extract_kb_facts.py {ts} из {source.name} -->\n\n"
        f"# База знаний MoveToRussia ({tag})\n\n"
        "Фактологический справочник: воронка, FAQ, процесс, часовые пояса. "
        "Инструкции для ИИ-агента — в `n8n_agent_prompt.md`.\n\n"
    )
    kb_path.write_text(kb_header + kb_body + "\n", encoding="utf-8", newline="\n")

    prompt_path = kb_dir / "n8n_agent_prompt.md"
    prompt_body = build_agent_prompt_md(sections)
    prompt_header = (
        f"<!-- Извлечено extract_kb_facts.py {ts} из {source.name} -->\n\n"
        f"# Промпт для ИИ-агента MoveToRussia ({tag}, n8n)\n\n"
        "Операционная инструкция для LLM. Факты и FAQ — в `movetorussia_kb.md`.\n\n"
    )
    prompt_path.write_text(
        prompt_header + prompt_body + "\n", encoding="utf-8", newline="\n"
    )
    return kb_path, prompt_path


def extract_version(version: int) -> tuple[Path, Path]:
    return extract_dir(KB_ROOT / f"v{version}", label=f"v{version}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        type=int,
        nargs="+",
        choices=[1, 2, 3, 4],
        default=[1, 2, 3],
        help="Версии для извлечения (по умолчанию 1 2 3)",
    )
    p.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Каталог с movetorussia_agent_kb.md (например knowledge_base/v1_clean)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.dir:
        kb_path, prompt_path = extract_dir(args.dir.resolve())
        print(f"{args.dir.name}: {kb_path.name}, {prompt_path.name}")
        return 0
    for v in args.only:
        kb_path, prompt_path = extract_version(v)
        kb_len = len(kb_path.read_text(encoding="utf-8"))
        prompt_len = len(prompt_path.read_text(encoding="utf-8"))
        print(
            f"v{v}: {kb_path.name} ({kb_len:,} симв.), "
            f"{prompt_path.name} ({prompt_len:,} симв.)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

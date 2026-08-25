#!/usr/bin/env python3
"""Генерация логической схемы Mail Agent (N8N workflow, пошагово с условиями)."""

from __future__ import annotations

from pathlib import Path

from graphviz import Digraph

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def build_logical_diagram() -> Digraph:
    dot = Digraph(
        "mail_agent_logical_flow",
        format="svg",
        engine="dot",
        graph_attr={
            "rankdir": "TB",
            "fontsize": "11",
            "fontname": "Segoe UI",
            "label": "MovetoRussia Mail Agent — пошаговая логика работы",
            "labelloc": "t",
            "bgcolor": "white",
            "pad": "0.5",
            "nodesep": "0.35",
            "ranksep": "0.55",
        },
        node_attr={
            "fontname": "Segoe UI",
            "fontsize": "10",
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#eceff1",
        },
        edge_attr={
            "fontname": "Segoe UI",
            "fontsize": "9",
            "color": "#37474f",
        },
    )

    def start(node_id: str, label: str) -> None:
        dot.node(node_id, label, shape="ellipse", fillcolor="#c8e6c9")

    def process(node_id: str, label: str, color: str = "#eceff1") -> None:
        dot.node(node_id, label, fillcolor=color)

    def decision(node_id: str, label: str) -> None:
        dot.node(node_id, label, shape="diamond", fillcolor="#fff9c4")

    def error_sink(node_id: str, label: str) -> None:
        dot.node(node_id, label, fillcolor="#ffcdd2")

    def end_node(node_id: str, label: str) -> None:
        dot.node(node_id, label, shape="ellipse", fillcolor="#b2dfdb")

    start(
        "s1",
        "СТАРТ\nМенеджер добавил строку\nв таблицу · проверка раз в минуту",
    )
    process(
        "s2",
        "Извлечение email клиента\nиз последней строки таблицы",
    )
    decision("d1", "Email клиента\nуказан?")
    process(
        "s3",
        "Поиск клиента в CRM\nпо email\n(узел: CRM Deal Lookup)",
    )
    decision("d2", "CRM нашла\nклиента?")
    process(
        "s4",
        "Получение ID сделки\nиз ответа CRM\n(узел: Extract Deal ID)",
    )
    process(
        "s5",
        "Загрузка карточки сделки\nиз CRM\n(узел: CRM Deal Get)",
    )
    decision("d3", "Карточка\nзагружена?")
    process(
        "s6",
        "Извлечение данных клиента\nимя · национальность · этап воронки\nпервое обращение · ID менеджера\n(узел: Extract Deal Data)",
        color="#e1bee7",
    )
    process(
        "s7",
        "Определение почты менеджера\nпо ID сотрудника в CRM\n(узел: Map Manager Email)",
        color="#ffe0b2",
    )
    process(
        "s8",
        "Загрузка переписки\nиз почты менеджера\n(узел: IMAP Search)",
        color="#fff9c4",
    )
    decision("d4", "Переписка\nзагружена?")
    process(
        "s9",
        "Выбор последнего письма\nс непустым текстом\n(узел: extract last mail)",
        color="#fff9c4",
    )
    decision("d5", "Письма\nнайдены?")
    process(
        "s10",
        "Сборка истории переписки\nхронология: клиент / менеджер\n(узел: Format Email History)",
        color="#fff9c4",
    )
    process(
        "s11",
        "Подготовка задания для ИИ\nправила · данные CRM · переписка\nпримеры хороших ответов\n(узел: Assemble Prompt)",
        color="#f3e5f5",
    )
    process(
        "s12",
        "Генерация ответа нейросетью\nDeepSeek · deepseek-v4-pro · T=0.4\n(узел: Basic LLM Chain)",
        color="#b2dfdb",
    )
    decision("d6", "Ответ\nсгенерирован?")
    process(
        "s13",
        "Запись в таблицу\nчерновик · последнее письмо\n(узел: Update Google Sheets)",
        color="#c8e6c9",
    )
    end_node(
        "e1",
        "ФИНИШ\nМенеджер видит результат\nв Google Sheets",
    )

    process(
        "s9b",
        "Запасной вариант:\nпервое обращение клиента\nиз CRM (если почты нет)",
        color="#fff9c4",
    )

    error_sink(
        "err",
        "Запись ошибки в таблицу\nколонка «Техническая информация»\n(ветка сбоя)",
    )

    dot.edge("s1", "s2")
    dot.edge("s2", "d1")
    dot.edge("d1", "s3", label="да")
    dot.edge("d1", "e1", label="нет · стоп", style="dashed")
    dot.edge("s3", "d2")
    dot.edge("d2", "s4", label="да")
    dot.edge("d2", "err", label="нет", color="#c62828")
    dot.edge("s4", "s5")
    dot.edge("s5", "d3")
    dot.edge("d3", "s6", label="да")
    dot.edge("d3", "err", label="нет", color="#c62828")
    dot.edge("s6", "s7")
    dot.edge("s7", "s8")
    dot.edge("s8", "d4")
    dot.edge("d4", "s9", label="да")
    dot.edge(
        "d4",
        "s9",
        label="нет · продолжаем\nбез переписки",
        style="dashed",
        color="#ef6c00",
    )
    dot.edge("s9", "d5")
    dot.edge("d5", "s10", label="да")
    dot.edge("d5", "s9b", label="нет")
    dot.edge("s9b", "s10")
    dot.edge("s10", "s11")
    dot.edge("s11", "s12")
    dot.edge("s12", "d6")
    dot.edge("d6", "s13", label="да")
    dot.edge("d6", "err", label="нет", color="#c62828")
    dot.edge("s13", "e1")
    dot.edge("err", "e1", style="dashed", color="#c62828")

    return dot


def build_mermaid() -> str:
    return """# MovetoRussia Mail Agent — логическая схема

> Сгенерировано скриптом `scripts/generate_logical_diagram.py` из экспорта `Mail Agent.json`.

```mermaid
flowchart TB
    START(["СТАРТ<br/>новая строка в таблице"])
    PY["Извлечение email клиента"]
    D1{"Email указан?"}
    CRM1["Поиск клиента в CRM"]
    D2{"CRM нашла клиента?"}
    EXT_ID["Получение ID сделки"]
    CRM2["Загрузка карточки сделки"]
    D3{"Карточка загружена?"}
    EXT_DATA["Данные клиента:<br/>имя, этап, менеджер"]
    MAP["Почта ответственного менеджера"]
    IMAP["Загрузка переписки из почты"]
    D4{"Переписка загружена?"}
    LAST["Последнее письмо с текстом"]
    D5{"Письма найдены?"}
    FALL["Запасной вариант:<br/>первое обращение из CRM"]
    FMT["История переписки<br/>клиент / менеджер"]
    PROMPT["Задание для ИИ<br/>правила + контекст + примеры"]
    LLM["Генерация ответа<br/>DeepSeek · T=0.4"]
    D6{"Ответ сгенерирован?"}
    SHEET_OK["Запись черновика в таблицу"]
    SHEET_ERR["Запись ошибки в таблицу"]
    END(["ФИНИШ<br/>результат у менеджера"])

    START --> PY --> D1
    D1 -->|да| CRM1
    D1 -.->|нет · стоп| END
    CRM1 --> D2
    D2 -->|да| EXT_ID --> CRM2 --> D3
    D2 -->|нет| SHEET_ERR
    D3 -->|да| EXT_DATA --> MAP --> IMAP --> D4
    D3 -->|нет| SHEET_ERR
    D4 -->|да| LAST
    D4 -.->|нет · продолжаем| LAST
    LAST --> D5
    D5 -->|да| FMT
    D5 -->|нет| FALL --> FMT
    FMT --> PROMPT --> LLM --> D6
    D6 -->|да| SHEET_OK --> END
    D6 -->|нет| SHEET_ERR --> END
```

## Пошаговое описание

| Шаг | Что происходит | Условие / ветвление | Узел N8N (для реализации) |
|-----|----------------|---------------------|---------------------------|
| 1 | Менеджер добавляет строку в таблицу; сценарий проверяет её раз в минуту | — | Google Sheets Trigger |
| 2 | Из последней строки берётся email клиента | Если email пустой — сценарий останавливается | Code in Python |
| 3 | CRM ищет клиента по email | Если CRM недоступна или клиент не найден — ошибка в таблицу | CRM Deal Lookup |
| 4 | Из ответа CRM извлекается ID сделки | Только если шаг 3 успешен | Extract Deal ID |
| 5 | По ID загружается полная карточка сделки | Если ошибка — запись в таблицу | CRM Deal Get |
| 6 | Из карточки берутся имя, национальность, этап воронки, первое обращение, ID менеджера | — | Extract Deal Data |
| 7 | По ID менеджера определяется его рабочая почта | Если ID не в справочнике — подставляется значение по умолчанию | Map Manager Email |
| 8 | Из почты менеджера загружается переписка с клиентом | Если почта недоступна — процесс **не останавливается**, идём дальше без переписки | IMAP Search |
| 9 | Выбирается последнее письмо с непустым текстом | Если писем нет — берётся первое обращение клиента из CRM | extract last mail |
| 10 | Вся переписка собирается в хронологический текст «клиент / менеджер» | — | Format Email History |
| 11 | Формируется задание для ИИ: правила общения, данные клиента, история, 3 эталонных примера | — | Assemble Prompt |
| 12 | DeepSeek генерирует текст следующего письма на английском (temperature 0.4) | Если ошибка — запись в таблицу | Basic LLM Chain |
| 13 | В таблицу записываются черновик ответа, последнее письмо и при необходимости текст ошибки | Успех и сбой оба завершаются у менеджера в таблице | Update Google Sheets |

## Что попадает в таблицу

| Колонка | Что видит менеджер | Откуда берётся |
|---------|-------------------|----------------|
| **client_email** | Email клиента | Из строки, которую добавил менеджер |
| **Neznajka_response** | Черновик ответа клиенту | Сгенерированный текст нейросети |
| **Последнее письмо** | Последнее сообщение в диалоге | Из почты или, если почты нет, из CRM |
| **Техническая информация** | Текст ошибки для разработчика | При сбое CRM, почты или генерации |

## Если что-то пошло не так

| Этап | Что делает система |
|------|-------------------|
| CRM (поиск или карточка) | Останавливает основной путь, пишет ошибку в таблицу |
| Почта | Не останавливает процесс: ИИ работает с данными из CRM |
| Генерация ответа | Пишет ошибку в таблицу, черновик не появляется |
| Сопоставление менеджера | Ошибка не блокирует сценарий (используется значение по умолчанию) |
"""


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)

    dot = build_logical_diagram()
    svg_path = DOCS / "logical_flow_diagram"
    dot.render(str(svg_path), cleanup=True)
    print(f"SVG: {svg_path}.svg")

    png_dot = build_logical_diagram()
    png_dot.format = "png"
    png_path = DOCS / "logical_flow_diagram"
    png_dot.render(str(png_path), cleanup=True)
    print(f"PNG: {png_path}.png")

    md_path = DOCS / "logical_flow_diagram.md"
    md_path.write_text(build_mermaid(), encoding="utf-8")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()

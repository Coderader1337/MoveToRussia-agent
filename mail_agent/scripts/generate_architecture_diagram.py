#!/usr/bin/env python3
"""Генерация архитектурной схемы Mail Agent (N8N workflow)."""

from __future__ import annotations

from pathlib import Path

from graphviz import Digraph

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def build_architecture_diagram() -> Digraph:
    dot = Digraph(
        "mail_agent_architecture",
        format="svg",
        engine="dot",
        graph_attr={
            "rankdir": "TB",
            "fontsize": "12",
            "fontname": "Segoe UI",
            "label": "MovetoRussia Mail Agent — как устроен агент",
            "labelloc": "t",
            "bgcolor": "white",
            "pad": "0.4",
            "nodesep": "0.6",
            "ranksep": "0.8",
        },
        node_attr={
            "fontname": "Segoe UI",
            "fontsize": "11",
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#f5f5f5",
        },
        edge_attr={
            "fontname": "Segoe UI",
            "fontsize": "10",
            "color": "#455a64",
        },
    )

    with dot.subgraph(name="cluster_user") as user:
        user.attr(
            label="Рабочее место менеджера",
            style="rounded,dashed",
            color="#81c784",
            bgcolor="#e8f5e9",
        )
        user.node(
            "manager",
            "Менеджер MovetoRussia\nтаблица Google Sheets «Neznajka»",
            fillcolor="#c8e6c9",
        )

    with dot.subgraph(name="cluster_n8n") as n8n:
        n8n.attr(
            label="N8N Cloud — сценарий автоматизации",
            style="rounded,dashed",
            color="#64b5f6",
            bgcolor="#e3f2fd",
        )
        n8n.node(
            "trigger",
            "Отслеживание таблицы\nновая строка · проверка раз в минуту",
            fillcolor="#bbdefb",
        )
        n8n.node(
            "pipeline",
            "Обработка заявки\nCRM → переписка → задание для ИИ → черновик",
            fillcolor="#90caf9",
        )
        n8n.node(
            "output",
            "Запись результата в таблицу\nчерновик · контекст · ошибки",
            fillcolor="#bbdefb",
        )
        n8n.edge("trigger", "pipeline")
        n8n.edge("pipeline", "output")

    with dot.subgraph(name="cluster_integrations") as integrations:
        integrations.attr(
            label="Подключённые системы",
            style="rounded,dashed",
            color="#ba68c8",
            bgcolor="#f3e5f5",
        )
        integrations.node(
            "crm",
            "CRM EnvyCRM\nпоиск клиента и карточка сделки",
            fillcolor="#e1bee7",
        )
        integrations.node(
            "imap_api",
            "Сервис загрузки переписки\nдоступ к почте менеджера",
            fillcolor="#fff9c4",
        )
        integrations.node(
            "yandex",
            "Яндекс Почта\nвходящие и отправленные письма",
            fillcolor="#fff59d",
        )
        integrations.node(
            "llm",
            "Нейросеть DeepSeek\nмодель deepseek-v4-pro",
            fillcolor="#b2dfdb",
        )
        integrations.edge("imap_api", "yandex", label="чтение\nпереписки")

    dot.edge("manager", "trigger", label="менеджер указывает\nemail клиента")
    dot.edge("output", "manager", label="готовый черновик\nответа клиенту")

    dot.edge("pipeline", "crm", label="email клиента")
    dot.edge("crm", "pipeline", label="имя, этап воронки,\nответственный менеджер")

    dot.edge("pipeline", "imap_api", label="почта менеджера\n+ email клиента")
    dot.edge("imap_api", "pipeline", label="вся история\nпереписки")

    dot.edge("pipeline", "llm", label="задание для ИИ:\nконтекст + правила + примеры")
    dot.edge("llm", "pipeline", label="текст письма\nна английском")

    return dot


def build_mermaid() -> str:
    return """# MovetoRussia Mail Agent — архитектурная схема

> Сгенерировано скриптом `scripts/generate_architecture_diagram.py` из экспорта `Mail Agent.json`.

```mermaid
flowchart TB
    subgraph USER["Рабочее место менеджера"]
        MGR["Менеджер<br/>таблица Google Sheets «Neznajka»"]
    end

    subgraph N8N["N8N Cloud — сценарий автоматизации"]
        TRG["Отслеживание таблицы<br/>новая строка · проверка раз в минуту"]
        PIPE["Обработка заявки<br/>CRM → переписка → задание для ИИ → черновик"]
        OUT["Запись результата в таблицу"]
        TRG --> PIPE --> OUT
    end

    subgraph EXT["Подключённые системы"]
        CRM["CRM EnvyCRM<br/>поиск клиента и карточка сделки"]
        MAIL_SVC["Сервис загрузки переписки"]
        MAIL["Яндекс Почта<br/>входящие и отправленные"]
        AI["Нейросеть DeepSeek<br/>deepseek-v4-pro"]
        MAIL_SVC --> MAIL
    end

    MGR -->|"email клиента"| TRG
    OUT -->|"черновик ответа"| MGR
    PIPE <-->|"данные клиента и сделки"| CRM
    PIPE <-->|"история переписки"| MAIL_SVC
    PIPE <-->|"задание для ИИ / текст письма"| AI
```

## Роли компонентов

| Компонент | Что делает | Для кого важно |
|-----------|------------|----------------|
| **Google Sheets «Neznajka»** | Менеджер вводит email клиента и получает черновик ответа, последнее письмо и текст ошибки, если что-то пошло не так. | Менеджер, CEO |
| **N8N Cloud** | Связывает все системы в один сценарий: запуск, сбор данных, подготовка задания для ИИ, запись результата. | Реализация |
| **CRM EnvyCRM** | Находит клиента по email, отдаёт имя, национальность, этап воронки, первое обращение и ID ответственного менеджера. | CEO, менеджер |
| **Сервис загрузки переписки** | По почте менеджера собирает всю переписку с клиентом (входящие и отправленные). | Реализация |
| **DeepSeek** | На основе контекста, правил общения и примеров пишет следующее письмо клиенту на английском. Температура модели: 0.4 (баланс точности и живости текста). | AI-консультант, CEO |
"""


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)

    dot = build_architecture_diagram()
    svg_path = DOCS / "architecture_diagram"
    dot.render(str(svg_path), cleanup=True)
    print(f"SVG: {svg_path}.svg")

    png_dot = build_architecture_diagram()
    png_dot.format = "png"
    png_path = DOCS / "architecture_diagram"
    png_dot.render(str(png_path), cleanup=True)
    print(f"PNG: {png_path}.png")

    md_path = DOCS / "architecture_diagram.md"
    md_path.write_text(build_mermaid(), encoding="utf-8")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()

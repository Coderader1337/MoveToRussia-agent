# MovetoRussia Mail Agent — Архитектурная диаграмма N8N Workflow

```mermaid
graph TD
    A["📊 Google Sheets<br/>Менеджер вводит<br/>email клиента"]
    
    B["🔍 N8N читает данные<br/>из Google Sheets"]
    A -->|API polling| B
    
    C["💼 Извлечение данных<br/>из CRM"]
    B -->|email клиента| C
    
    CRM["💼 CRM"]
    CRM -->|API| C
    
    D["🔗 Сопоставление ID<br/>Менеджер → Почтовый ящик"]
    C -->|ID менеджера| D
    
    E["📬 Получение истории<br/>переписки из почты"]
    D -->|email менеджера| E
    
    F["📮 Яндекс Mail"]
    F -->|IMAP| E
    
    G["🔧 Сборка промпта<br/>Запрос + история + инструкции"]
    E -->|Переписка| G
    C -->|Первоначальный запрос + метаданные| G
    
    H["📝 Шаблоны примеры<br/>Примеры идеального общения"]
    H -->|Шаблон промпта| G
    
    I["🤖 LLM<br/>DeepSeek/Claude/GPT"]
    G -->|Промпт| I
    
    J["📊 Google Sheet<br/>Результат в соседней ячейке"]
    I -->|API| J
    
    style A fill:#c8e6c9,stroke:#333,stroke-width:2px,color:#000
    style B fill:#bbdefb,stroke:#333,stroke-width:2px,color:#000
    style C fill:#f3e5f5,stroke:#333,stroke-width:2px,color:#000
    style CRM fill:#f3e5f5,stroke:#333,stroke-width:2px,color:#000
    style D fill:#ffe0b2,stroke:#333,stroke-width:2px,color:#000
    style E fill:#e8f5e9,stroke:#333,stroke-width:2px,color:#000
    style F fill:#fff9c4,stroke:#333,stroke-width:2px,color:#000
    style G fill:#fff9c4,stroke:#333,stroke-width:2px,color:#000
    style H fill:#fce4ec,stroke:#333,stroke-width:2px,color:#000
    style I fill:#e0f2f1,stroke:#333,stroke-width:2px,color:#000
    style J fill:#c8e6c9,stroke:#333,stroke-width:2px,color:#000
    
    linkStyle default stroke:#9ccc65,stroke-width:5px
```

## Этапы workflow:

| № | Компонент | Описание | Статус |
|---|-----------|---------|--------|
| 1 | **Google Sheets (вход)** | Менеджер вводит письмо клиента | 📋 План |
| 2 | **N8N читает Google Sheets** | Триггер обнаруживает новое письмо и забирает данные | 📋 План |
| 3 | **CRM Lookup** | Поиск первого сообщения от клиента в CRM, получение ID менеджера | 📋 План |
| 4 | **Сопоставление ID → Почта** | По ID менеджера определяется, с какого почтового ящика забирать историю (IMAP) | 📋 План |
| 5 | **IMAP получение истории** | Загрузка полной переписки с клиентом из определённого ящика | 📋 План |
| 6 | **Сборка промпта** | Объединение: текущее письмо + история переписки + шаблоны + инструкции | 📋 План |
| 7 | **LLM генерация** | LLM обрабатывает промпт и генерирует ответ | 📋 План |
| 8 | **Google Sheets (выход)** | Результат записывается в соседнюю ячейку для менеджера | 📋 План |

"""
Построение базы знаний (RAG) для ИИ-агента MoveToRussia из выгруженных переписок.

Вход:  mailbox_export/all_messages.jsonl  (см. export_manager_mailboxes.py)
Выход: knowledge_base/movetorussia_agent_kb.md  — универсальная инструкция-онбординг
       knowledge_base/_intermediate/*.json     — промежуточные извлечения (gitignored)

Два слоя анализа:
  1) Детерминированный (Python, без LLM): частоты часовых поясов, каналов/методов
     бронирования звонков, тематик вопросов клиентов, упоминаний шагов/виз/оплат.
  2) LLM map-reduce (DeepSeek): из репрезентативных двусторонних диалогов извлекаются
     FAQ, паттерны действий, стиль, факты о процессе; затем сводятся в единую инструкцию.

.env:
  DEEPSEEK_API_KEY, DEEPSEEK_MODEL (опц.), DEEPSEEK_TEMPERATURE (опц.)

Примеры:
  python build_knowledge_base.py
  python build_knowledge_base.py --skip-llm           # только детерминированный анализ
  python build_knowledge_base.py --max-threads 40
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "mailbox_export"
JSONL_PATH = EXPORT_DIR / "all_messages.jsonl"
KB_DIR = ROOT / "knowledge_base"
INTERMEDIATE_DIR = KB_DIR / "_intermediate"
KB_PATH = KB_DIR / "movetorussia_agent_kb.md"

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_TEMPERATURE = 0.2

# Бюджет символов на один диалог при map-фазе (голова + хвост диалога).
MAP_THREAD_CHAR_BUDGET = 14_000
# Сколько диалогов максимум отправлять в LLM (по убыванию объёма).
DEFAULT_MAX_THREADS = 60
# Порог объёда для иерархического reduce.
REDUCE_GROUP_CHAR_BUDGET = 45_000


# --------------------------------------------------------------------------- #
#  Утилиты окружения / DeepSeek
# --------------------------------------------------------------------------- #
def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default)
    return _strip_wrapping_quotes(raw) if isinstance(raw, str) else default


def call_deepseek(system_prompt: str, user_prompt: str, *, temperature: float) -> str:
    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Нужен DEEPSEEK_API_KEY в .env")
    model = _env("DEEPSEEK_MODEL") or DEEPSEEK_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"DeepSeek HTTP {e.code}: {err[:300]}")
            if e.code in (429, 500, 502, 503):
                time.sleep(3 * (attempt + 1))
                continue
            raise last_err from e
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"DeepSeek недоступен после повторов: {last_err}")


# --------------------------------------------------------------------------- #
#  Загрузка и группировка писем
# --------------------------------------------------------------------------- #
def load_messages() -> list[dict[str, Any]]:
    if not JSONL_PATH.is_file():
        raise FileNotFoundError(
            f"Нет {JSONL_PATH}. Сначала запустите export_manager_mailboxes.py"
        )
    msgs: list[dict[str, Any]] = []
    with JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msgs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return msgs


def group_threads(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_client: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in messages:
        by_client[m.get("counterpart", "")].append(m)
    for client, msgs in by_client.items():
        msgs.sort(key=lambda x: _ts_of(x))
    return by_client


def _ts_of(m: dict[str, Any]) -> float:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(m.get("date", "")).timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
#  Слой 1: детерминированный анализ
# --------------------------------------------------------------------------- #
TZ_ABBREVIATIONS = [
    "UTC", "GMT", "EST", "EDT", "CST", "CDT", "MST", "MDT", "PST", "PDT",
    "CET", "CEST", "EET", "EEST", "WET", "BST", "MSK", "IST", "AEST", "AEDT",
    "AWST", "ACST", "JST", "KST", "HKT", "SGT", "NZST", "NZDT", "AST", "NST",
    "CAT", "EAT", "WAT", "SAST", "ART", "BRT", "CLT", "COT", "PET",
]
# Аббревиатуры поясов матчим ТОЛЬКО в верхнем регистре, иначе re.I ловит
# обычные слова (art→ART, cat→CAT, eat→EAT, pet→PET и т.п.).
_TZ_RE = re.compile(
    r"\b(?:" + "|".join(TZ_ABBREVIATIONS) + r")\b|"
    r"\b(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::\d{2})?\b"
)

BOOKING_CHANNELS = {
    "WhatsApp": r"whats\s?app",
    "Telegram": r"telegram",
    "Zoom": r"\bzoom\b",
    "Google Meet": r"google meet|\bg-?meet\b",
    "Skype": r"\bskype\b",
    "Phone call": r"phone call|call you|give you a call|a quick call|jump on a call|hop on a call",
    "Video call": r"video call|video chat",
    "Calendly": r"calendly",
}

VISA_TERMS = {
    "Golden Visa": r"golden visa",
    "Shared Values Visa": r"shared values? visa|shared-values",
    "Temporary Residence (TRP)": r"temporary residence|\btrp\b|temporary residency",
    "Permanent Residence (PRP)": r"permanent residence|\bprp\b|permanent residency",
    "Citizenship": r"citizenship|naturali[sz]ation|passport",
    "Visa (general)": r"\bvisa\b",
    "Registration": r"\bregistration\b|migration registration",
}

TOPIC_KEYWORDS = {
    "Виза и правовой статус": [
        "visa", "residence", "permit", "trp", "prp", "citizenship", "passport",
        "registration", "migration", "apostille", "legalization", "document",
    ],
    "Оплата и стоимость": [
        "payment", "fee", "cost", "price", "invoice", "pay", "deposit",
        "installment", "transfer", "wire", "refund", "$", "usd", "eur",
    ],
    "Сроки и этапы процесса": [
        "step", "timeline", "how long", "process", "stage", "duration",
        "deadline", "when will", "next step", "appendix",
    ],
    "Звонки и расписание": [
        "call", "zoom", "whatsapp", "telegram", "meet", "schedule",
        "available", "time zone", "what time", "convenient",
    ],
    "Семья": [
        "family", "wife", "husband", "children", "daughter", "son",
        "spouse", "kids", "child", "married",
    ],
    "Логистика переезда": [
        "flight", "housing", "apartment", "rent", "move", "shipping",
        "pet", "school", "job", "work", "bank account", "sim", "relocat",
    ],
    "Безопасность и чувствительные темы": [
        "safe", "safety", "war", "sanction", "politic", "military",
        "conscription", "dangerous", "risk",
    ],
}

STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "does", "did", "to", "of", "in",
    "for", "on", "and", "or", "if", "i", "you", "we", "my", "your", "me",
    "it", "this", "that", "be", "can", "could", "would", "will", "have",
    "has", "with", "what", "when", "how", "where", "which", "there", "any",
    "so", "as", "at", "by", "from", "but", "not", "they", "he", "she",
    "please", "thank", "thanks", "dear", "hi", "hello", "am", "was", "were",
}


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return re.split(r"(?<=[.!?])\s+", text)


def analyze_deterministic(
    messages: list[dict[str, Any]],
    threads: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    tz_counter: Counter[str] = Counter()
    channel_counter: Counter[str] = Counter()
    visa_counter: Counter[str] = Counter()
    topic_counter: Counter[str] = Counter()
    question_bucket: Counter[str] = Counter()
    question_examples: dict[str, str] = {}
    booking_sentences: Counter[str] = Counter()

    per_manager: Counter[str] = Counter()
    incoming = [m for m in messages if m.get("direction") == "incoming"]
    outgoing = [m for m in messages if m.get("direction") == "outgoing"]

    for m in messages:
        per_manager[m.get("mailbox_account", "")] += 1
        text = m.get("text", "") or ""
        low = text.lower()

        for tz in {x.group(0).upper().replace(" ", "") for x in _TZ_RE.finditer(text)}:
            tz_counter[tz] += 1
        for name, pat in BOOKING_CHANNELS.items():
            if re.search(pat, low):
                channel_counter[name] += 1
        for name, pat in VISA_TERMS.items():
            if re.search(pat, low):
                visa_counter[name] += 1
        for topic, kws in TOPIC_KEYWORDS.items():
            if any(k in low for k in kws):
                topic_counter[topic] += 1

    # Методы бронирования звонков — из писем менеджера (исходящие).
    # Узкий шаблон именно про назначение звонка (а не слоган «we call ...»).
    booking_intent = re.compile(
        r"(schedule a (?:brief |short |quick )?call|arrange a call|book a call|"
        r"set up a call|hop on a call|jump on a call|quick call|brief call|"
        r"onboarding call|call at your convenience|let'?s schedule|"
        r"what time works|your time zone|convenient time|available for a call|"
        r"propose (?:a )?(?:few )?slots?|suggest (?:a )?(?:few )?time)",
        re.I,
    )
    # Шумовые маркеры: подписи и процитированные шапки писем.
    booking_noise = re.compile(
        r"kind regards|client relationship manager|movetorussia\.com|----|"
        r"кому:|\bto:|subject:|тема:|stay connected|social media|@arkvostok",
        re.I,
    )
    for m in outgoing:
        for s in _sentences(m.get("text", "") or ""):
            s = re.sub(r"\s+", " ", s).strip()
            if 25 <= len(s) <= 220 and booking_intent.search(s) and not booking_noise.search(s):
                booking_sentences[s] += 1

    # Вопросы клиентов — из входящих писем.
    for m in incoming:
        for s in _sentences(m.get("text", "") or ""):
            s = s.strip()
            if not s.endswith("?") or not (10 <= len(s) <= 220):
                continue
            words = re.findall(r"[a-zA-Z']+", s.lower())
            key_words = [w for w in words if w not in STOPWORDS and len(w) > 2]
            if not key_words:
                continue
            key = " ".join(sorted(set(key_words))[:6])
            question_bucket[key] += 1
            question_examples.setdefault(key, s)

    top_questions = [
        {"count": c, "example": question_examples[k]}
        for k, c in question_bucket.most_common(40)
    ]

    return {
        "totals": {
            "messages": len(messages),
            "incoming": len(incoming),
            "outgoing": len(outgoing),
            "clients": len(threads),
            "two_way_clients": sum(
                1
                for ms in threads.values()
                if any(x["direction"] == "outgoing" for x in ms)
                and any(x["direction"] == "incoming" for x in ms)
            ),
            "per_manager": dict(per_manager),
        },
        "timezones": tz_counter.most_common(25),
        "booking_channels": channel_counter.most_common(),
        "visa_terms": visa_counter.most_common(),
        "topics": topic_counter.most_common(),
        "top_client_questions": top_questions,
        "booking_phrases": booking_sentences.most_common(30),
    }


# --------------------------------------------------------------------------- #
#  Слой 2: LLM map-reduce
# --------------------------------------------------------------------------- #
def _render_thread(client: str, msgs: list[dict[str, Any]], budget: int) -> str:
    lines: list[str] = []
    for m in msgs:
        who = "MANAGER" if m["direction"] == "outgoing" else "CLIENT"
        body = re.sub(r"\n{3,}", "\n\n", (m.get("text") or "").strip())
        lines.append(f"--- {who} ({m.get('date','')}) ---\n{body}")
    full = "\n\n".join(lines)
    if len(full) <= budget:
        return full
    head = full[: int(budget * 0.6)]
    tail = full[-int(budget * 0.4) :]
    return head + "\n\n...[середина диалога опущена]...\n\n" + tail


MAP_SYSTEM = (
    "Ты — методолог отдела клиентского сервиса MoveToRussia.com (помогаем "
    "гражданам недружественных стран переехать в Россию). Анализируешь реальные "
    "переписки менеджеров с клиентами, чтобы извлечь знания для обучения нового "
    "сотрудника. Отвечай строго в заданном формате, на русском, без воды. "
    "Не выдумывай факты — бери только то, что есть в переписке."
)

MAP_TEMPLATE = """Проанализируй один реальный диалог менеджера с клиентом.
Извлеки знания в формате Markdown ровно по этим разделам (если данных нет — пиши «—»):

### Этап(ы) воронки в диалоге
### Вопросы клиента и как менеджер на них ответил
(маркированный список: «В: ... → О: ...», ответы — фактологично)
### Факты о процессе переезда/визах/документах/оплатах
(конкретика: суммы, шаги, сроки, названия, условия — только из текста)
### Часовой пояс и согласование звонка
(как клиент назвал свой пояс/город, как договаривались о звонке, канал связи)
### Паттерны действий менеджера
(что менеджер делает проактивно, последовательность шагов)
### Стиль и тон
(приёмы, обороты, как обрабатываются чувствительные темы)

ДИАЛОГ С КЛИЕНТОМ {client}:
{thread}
"""

REDUCE_SYSTEM = (
    "Ты — ведущий методолог MoveToRussia.com. Из множества разрозненных заметок "
    "по реальным диалогам ты составляешь ЕДИНУЮ базу знаний и инструкцию для "
    "нового сотрудника — ИИ-агента на n8n, который ведёт первичную переписку с "
    "лидами. Пиши на русском, структурировано, конкретно и практично. "
    "Объединяй повторяющееся, убирай противоречия, сохраняй все фактические данные "
    "(суммы, сроки, шаги, названия виз, каналы связи). Ничего не выдумывай."
)

REDUCE_TEMPLATE = """Ниже — заметки, извлечённые из реальных диалогов менеджеров с клиентами.
Составь на их основе фрагмент базы знаний для ИИ-агента MoveToRussia.

Сделай разделы:
## FAQ — частые вопросы клиентов и эталонные ответы
## Факты о процессе переезда (визы, статусы, документы, оплаты, сроки)
## Работа с часовыми поясами и бронирование звонков
## Паттерны действий по этапам воронки
## Стиль общения и работа с чувствительными темами

ЗАМЕТКИ:
{notes}
"""

FINAL_SYSTEM = (
    "Ты — ведущий методолог MoveToRussia.com. Составляешь единую базу знаний и "
    "операционную инструкцию для УНИВЕРСАЛЬНОГО ИИ-менеджера на n8n. Этот агент "
    "ведёт клиента на ЛЮБОЙ стадии воронки (от первого обращения до сопровождения "
    "после переезда) и на каждом письме должен выдавать релевантный следующий ответ. "
    "Документ целиком вставляется в промпт LLM перед каждым письмом, поэтому он "
    "должен быть самодостаточным справочником и playbook'ом одновременно. "
    "Пиши на русском, плотно, конкретно, без воды. Сохраняй ВСЕ фактические данные "
    "(суммы, сроки, шаги, названия виз, каналы, ссылки). Ничего не выдумывай."
)

FINAL_TEMPLATE = """У тебя есть один или несколько сводных фрагментов базы знаний,
извлечённых из реальных диалогов менеджеров с клиентами на разных стадиях.
Объедини их в ИТОГОВУЮ инструкцию для УНИВЕРСАЛЬНОГО ИИ-менеджера MoveToRussia.

Это не онбординг новичка, а постоянный справочник: агент уже работает и на каждой
стадии воронки пишет следующее письмо клиенту. Документ цельный, без дублей, в Markdown.
Структура:

# База знаний и операционная инструкция универсального ИИ-менеджера MoveToRussia

## 1. Роль и принцип работы
(агент — универсальный менеджер; на каждом письме: 1) определи стадию воронки по истории,
2) ответь на все открытые вопросы клиента, 3) сделай следующий целесообразный шаг.
Цель — довести клиента до оплаты и успешного переезда, метрика — оценка сервиса 10/10.)

## 2. Playbook по стадиям воронки
(для КАЖДОЙ стадии: как её распознать по переписке → какова цель письма на этой стадии →
что включить в письмо → типичные формулировки. Покрой стадии: Запрос/Первый контакт;
Ответ на вопросы; Предложение созвона; Подтверждение и проведение созвона; Отправка
предложения/договора/счёта; Согласование условий и оплата; Онбординг после оплаты;
Сопровождение процесса (документы, поездка, регистрация); Пост-переезд и доп.услуги;
Реактивация при паузе/отказе.)

## 3. FAQ: вопросы клиентов и эталонные ответы
## 4. Факты о процессе переезда (визы, статусы, документы, оплаты, сроки, переводы средств)
## 5. Часовые пояса и бронирование звонков (методики, каналы, формулировки)
## 6. Паттерны проактивности и удержания
## 7. Стиль общения, тон, работа с чувствительными темами
## 8. Стоп-правила (чего агент НЕ делает)

Сохрани всю конкретику. Раздел 2 (playbook по стадиям) — самый важный, проработай его детально.

ФРАГМЕНТЫ:
{fragments}
"""


def _map_one(
    i: int,
    total: int,
    client: str,
    msgs: list[dict[str, Any]],
    temperature: float,
    log_lock: Lock,
) -> tuple[int, str | None]:
    cache = INTERMEDIATE_DIR / f"map_{i:03d}.json"
    if cache.is_file():
        try:
            note = json.loads(cache.read_text(encoding="utf-8"))["note"]
            with log_lock:
                print(f"  Map {i}/{total} (из кэша)")
            return i, note
        except Exception:
            pass
    thread_text = _render_thread(client, msgs, MAP_THREAD_CHAR_BUDGET)
    user = MAP_TEMPLATE.format(client=_anon(client), thread=thread_text)
    try:
        note = call_deepseek(MAP_SYSTEM, user, temperature=temperature)
    except Exception as exc:
        with log_lock:
            print(f"  Map {i}/{total} ошибка: {exc}", file=sys.stderr)
        return i, None
    cache.write_text(
        json.dumps({"client": _anon(client), "note": note}, ensure_ascii=False),
        encoding="utf-8",
    )
    with log_lock:
        print(f"  Map {i}/{total} готово ({len(note)} симв.)")
    return i, note


def run_map(
    threads: dict[str, list[dict[str, Any]]],
    *,
    max_threads: int,
    temperature: float,
    workers: int,
) -> list[str]:
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    # Только двусторонние диалоги, по убыванию числа писем (богаче контент).
    two_way = {
        c: ms
        for c, ms in threads.items()
        if any(x["direction"] == "outgoing" for x in ms)
        and any(x["direction"] == "incoming" for x in ms)
    }
    ranked = sorted(two_way.items(), key=lambda kv: len(kv[1]), reverse=True)
    ranked = ranked[:max_threads]
    total = len(ranked)
    print(
        f"  Map: диалогов к обработке: {total} (из {len(two_way)} двусторонних), "
        f"параллельно: {workers}"
    )

    log_lock = Lock()
    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_map_one, i, total, client, msgs, temperature, log_lock)
            for i, (client, msgs) in enumerate(ranked, start=1)
        ]
        for fut in as_completed(futures):
            idx, note = fut.result()
            if note:
                results[idx] = note
    # Сохраняем исходный порядок (по убыванию объёма диалога).
    return [results[i] for i in sorted(results)]


def _anon(email_addr: str) -> str:
    """Обезличиваем клиента: оставляем только домен для контекста."""
    if "@" in email_addr:
        return "client@" + email_addr.split("@", 1)[1]
    return "client"


def _group_by_budget(items: list[str], budget: int) -> list[list[str]]:
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for it in items:
        if cur and cur_len + len(it) > budget:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(it)
        cur_len += len(it)
    if cur:
        groups.append(cur)
    return groups


def run_reduce(notes: list[str], *, temperature: float, force: bool = False) -> str:
    if not notes:
        return ""
    final_cache = INTERMEDIATE_DIR / "final.md"
    if final_cache.is_file() and not force:
        print("  Final: из кэша")
        return final_cache.read_text(encoding="utf-8")

    groups = _group_by_budget(notes, REDUCE_GROUP_CHAR_BUDGET)
    print(f"  Reduce: групп заметок: {len(groups)}")
    fragments: list[str] = []
    for gi, group in enumerate(groups, start=1):
        frag_cache = INTERMEDIATE_DIR / f"reduce_{gi:02d}.md"
        if frag_cache.is_file() and not force:
            fragments.append(frag_cache.read_text(encoding="utf-8"))
            print(f"  Reduce {gi}/{len(groups)} (из кэша)")
            continue
        joined = "\n\n---\n\n".join(group)
        user = REDUCE_TEMPLATE.format(notes=joined)
        frag = call_deepseek(REDUCE_SYSTEM, user, temperature=temperature)
        frag_cache.write_text(frag, encoding="utf-8")
        fragments.append(frag)
        print(f"  Reduce {gi}/{len(groups)} готово ({len(frag)} симв.)")

    print("  Final: сборка итоговой инструкции")
    final = call_deepseek(
        FINAL_SYSTEM,
        FINAL_TEMPLATE.format(fragments="\n\n=====\n\n".join(fragments)),
        temperature=temperature,
    )
    final_cache.write_text(final, encoding="utf-8")
    return final


# --------------------------------------------------------------------------- #
#  Сборка MD
# --------------------------------------------------------------------------- #
def render_data_appendix(stats: dict[str, Any]) -> str:
    t = stats["totals"]
    lines: list[str] = []
    lines.append("## Приложение: данные из анализа переписки (детерминированный слой)")
    lines.append("")
    lines.append(
        f"Проанализировано писем: **{t['messages']}** "
        f"(входящих {t['incoming']}, исходящих {t['outgoing']}), "
        f"клиентов: **{t['clients']}**, двусторонних диалогов: "
        f"**{t['two_way_clients']}**."
    )
    lines.append("")

    def table(title: str, rows: list[tuple], headers: tuple[str, str]) -> None:
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"| {headers[0]} | {headers[1]} |")
        lines.append("| --- | ---: |")
        for name, cnt in rows:
            safe = str(name).replace("|", "\\|")
            lines.append(f"| {safe} | {cnt} |")
        lines.append("")

    table("Упоминания часовых поясов (топ)", stats["timezones"], ("Часовой пояс", "Писем"))
    table("Каналы/методы связи для звонков", stats["booking_channels"], ("Канал", "Писем"))
    table("Типы виз/статусов", stats["visa_terms"], ("Термин", "Писем"))
    table("Тематики переписки", stats["topics"], ("Тема", "Писем"))

    lines.append("### Частые вопросы клиентов (по кластерам ключевых слов)")
    lines.append("")
    for q in stats["top_client_questions"][:30]:
        ex = q["example"].replace("|", "\\|")
        lines.append(f"- ({q['count']}×) {ex}")
    lines.append("")

    lines.append("### Типичные формулировки менеджеров о звонке")
    lines.append("")
    for phrase, cnt in stats["booking_phrases"][:20]:
        lines.append(f"- ({cnt}×) {phrase}")
    lines.append("")
    return "\n".join(lines)


def write_kb(final_md: str, stats: dict[str, Any]) -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- Сгенерировано build_knowledge_base.py "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} из реальных переписок -->\n\n"
    )
    body_parts = [header]
    if final_md.strip():
        body_parts.append(final_md.strip())
        body_parts.append("\n\n---\n")
    else:
        body_parts.append("# База знаний MoveToRussia (LLM-слой пропущен)\n")
    body_parts.append(render_data_appendix(stats))
    KB_PATH.write_text("\n".join(body_parts).rstrip() + "\n", encoding="utf-8", newline="\n")

    (KB_DIR / "analysis_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-llm", action="store_true", help="Только детерминированный анализ")
    p.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    p.add_argument("--workers", type=int, default=6, help="Параллельных запросов к DeepSeek")
    p.add_argument("--temperature", type=float, default=DEEPSEEK_TEMPERATURE)
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    print("Загрузка писем…")
    messages = load_messages()
    threads = group_threads(messages)
    print(f"  Писем: {len(messages)} | клиентов: {len(threads)}")

    print("Детерминированный анализ…")
    stats = analyze_deterministic(messages, threads)

    final_md = ""
    if not args.skip_llm:
        print("LLM map-фаза…")
        t0 = time.time()
        notes = run_map(
            threads,
            max_threads=args.max_threads,
            temperature=args.temperature,
            workers=args.workers,
        )
        print(f"  Map готов: {len(notes)} заметок за {time.time()-t0:.0f} c")
        print("LLM reduce-фаза…")
        final_md = run_reduce(notes, temperature=args.temperature)

    write_kb(final_md, stats)
    print(f"\nГотово. База знаний: {KB_PATH}")
    print(f"Статистика: {KB_DIR / 'analysis_stats.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

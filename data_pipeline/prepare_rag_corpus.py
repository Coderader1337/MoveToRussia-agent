#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_rag_corpus.py
======================

Готовит корпус переписок MoveToRussia для RAG:
  1. Парсит .txt-файлы формата "КЛИЕНТ / ПИСЕМ / [РОЛЬ] ... ----- тело письма"
  2. Чистит тело каждого письма:
       - вырезает "хвосты" цитат ("X schrieb am ...:", "On ... wrote:", "X написал(а):")
       - вырезает повторяющиеся подписи менеджеров (блок с "MovetoRussia.com")
       - нормализует пробелы/переносы строк
  3. Группирует письма в "обмены" (exchange) = связка вопрос(ы) клиента + ответ(ы) менеджера —
     это и есть смысловая единица для эмбеддинга, а не отдельное письмо.
  4. Помечает малоинформативные обмены флагом low_signal (не удаляет!).
  5. Пишет результат:
       mailbox_export_RAG/threads/<файл>.json   — по одному на тред, с полным деревом писем + exchanges
       mailbox_export_RAG/corpus.jsonl          — плоский список chunks, готовый для индексации в Qdrant
       mailbox_export_RAG/prepare_report.txt    — отчёт по чистке (счётчики + примеры срезанного текста)

Опционально (--distill): прогоняет каждый exchange через DeepSeek API,
чтобы получить компактную "карточку знания" (ситуация клиента + суть ответа)
в дополнение к очищенному сырому тексту. Требует переменную окружения DEEPSEEK_API_KEY.

Использование:
    python prepare_rag_corpus.py
    python prepare_rag_corpus.py --input mailbox_export_clean/threads --output mailbox_export_RAG/threads
    python prepare_rag_corpus.py --distill --distill-limit 500

По умолчанию скрипт считает, что запускается из директории, где лежит папка
mailbox_export_clean, и кладёt результат в СОСЕДНЮЮ папку mailbox_export_RAG
(как и было запрошено) — сам вычисляет путь, ничего вручную задавать не нужно.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Опциональный langdetect: если не установлен — просто не проставляем язык
# --------------------------------------------------------------------------
try:
    from langdetect import detect as _langdetect_detect  # type: ignore
    from langdetect import DetectorFactory as _LDFactory  # type: ignore

    _LDFactory.seed = 0  # детерминированность
    HAVE_LANGDETECT = True
except ImportError:
    HAVE_LANGDETECT = False


# ==========================================================================
# Регулярки формата письма
# ==========================================================================

SEPARATOR_RE = re.compile(r"(?m)^=+\s*$")
DASH_LINE_RE = re.compile(r"(?m)^-{5,}\s*$")

ROLE_LINE_RE = re.compile(r"^\[(?P<role>[^\]]+)\]\s*\((?P<role_email>[^)]*)\)\s*$")

HEADER_LABELS = {
    "Тема:": "subject",
    "От:": "from_",
    "Кому:": "to",
    "Дата:": "date",
}


def parse_message_header(block: str) -> tuple[Optional[dict], str]:
    """Построчно разбирает заголовок письма.

    В отличие от жёсткого regex (Тема/От/Кому/Дата строго по одной строке
    каждое), эта версия толерантна к:
      - длинным темам/полям, перенесённым на следующую строку (частый кейс
        в реальных экспортах — например, длинная тема письма);
      - лишним пустым строкам между полями заголовка;
      - любому порядку полей (хотя обычно он фиксирован).
    Останавливается на первой строке-разделителе "----...". Всё после неё —
    тело письма (возвращается как есть, без изменений).

    Возвращает (header_dict_или_None, body_raw). Если распарсить не удалось —
    header_dict = None, а body_raw = весь исходный блок (чтобы вызывающий код
    мог залогировать проблему и НЕ терять письмо целиком).
    """
    lines = block.split("\n")
    if not lines:
        return None, block

    role_match = ROLE_LINE_RE.match(lines[0].strip())
    if not role_match:
        return None, block

    fields = {"subject": "", "from_": "", "to": "", "date": ""}
    current_key: Optional[str] = None
    body_start_idx: Optional[int] = None

    for i in range(1, len(lines)):
        stripped = lines[i].strip()

        if DASH_LINE_RE.match(stripped):
            body_start_idx = i + 1
            break

        matched_key = None
        for label, key in HEADER_LABELS.items():
            if stripped.startswith(label):
                matched_key = key
                fields[key] = stripped[len(label):].strip()
                current_key = key
                break

        if matched_key is None and current_key is not None and stripped:
            # продолжение предыдущего поля, перенесённое на следующую строку
            fields[current_key] = (fields[current_key] + " " + stripped).strip()

    if body_start_idx is None:
        # не нашли разделитель "----" — не можем достоверно отделить
        # заголовок от тела, безопаснее пропустить с предупреждением
        return None, block

    header = {
        "role": normalize_role(role_match.group("role")),
        "role_email": role_match.group("role_email").strip(),
        "subject": fields["subject"],
        "from_": fields["from_"],
        "to": fields["to"],
        "date": fields["date"],
    }
    body_raw = "\n".join(lines[body_start_idx:])
    return header, body_raw


CLIENT_HEADER_RE = re.compile(r"КЛИЕНТ:\s*(.+)")
COUNT_HEADER_RE = re.compile(r"ПИСЕМ:\s*(\d+)")

ROLE_MANAGER = "manager"
ROLE_CLIENT = "client"


def normalize_role(raw: str) -> str:
    raw = raw.strip().upper()
    if raw.startswith("МЕНЕДЖЕР"):
        return ROLE_MANAGER
    if raw.startswith("КЛИЕНТ"):
        return ROLE_CLIENT
    return "unknown"


# ==========================================================================
# Очистка тела письма
# ==========================================================================

# Ключевые слова, которыми обычно начинается "хвост" цитаты предыдущего письма
# (сама цитата уже вырезана при экспорте, остался только вводный оборот).
QUOTE_TAIL_KEYWORDS = (
    "schrieb",       # нем. "X schrieb am ...:"
    " wrote",        # англ. "On ... X wrote:"
    "написал",       # рус.
    "napisał",       # польск.
    "escribió",      # исп.
    "a écrit",       # франц.
    "ha scritto",    # итал.
)

MAX_QUOTE_TAIL_PARA_LEN = 260  # макс. длина последнего абзаца, чтобы считать его хвостом цитаты


def strip_quote_tail(body: str) -> tuple[str, Optional[str]]:
    """Срезает висящий вводный оборот цитаты в конце письма, если он там есть.

    Возвращает (очищенное_тело, вырезанный_фрагмент_или_None).
    Логика: берём именно ПОСЛЕДНИЙ абзац письма (после последнего разрыва
    пустой строкой) и проверяем ТОЛЬКО его — короткий ли он, заканчивается ли
    двоеточием и содержит ли одно из ключевых слов ("schrieb", "wrote",
    "написал"...). Одиночные переносы строк внутри абзаца (перенос имени/даты
    при wrap) не мешают, т.к. абзацы бьются только по пустой строке.
    """
    stripped = body.rstrip()
    if not stripped:
        return body, None

    para_break = stripped.rfind("\n\n")
    tail_para = stripped[para_break + 2:] if para_break != -1 else stripped
    tail_para_stripped = tail_para.strip()

    if not tail_para_stripped or len(tail_para_stripped) > MAX_QUOTE_TAIL_PARA_LEN:
        return body, None

    if not re.search(r":\s*\Z", tail_para_stripped):
        return body, None

    if not any(kw in tail_para_stripped.lower() for kw in QUOTE_TAIL_KEYWORDS):
        return body, None

    cut_at = para_break if para_break != -1 else 0
    cleaned = stripped[:cut_at].rstrip()
    if not cleaned:
        # похоже, всё письмо — это одна цитата-хвост; не рискуем стирать всё
        return body, None
    return cleaned, tail_para_stripped


SIGNATURE_MARKER = "MovetoRussia.com"

# Якорь начала авто-подписи: "--" (одно-три дефиса, возможен nbsp/пробелы) + "Kind regards".
# Судя по корпусу, эта авто-подпись у компании ВСЕГДА на английском (даже в письмах на
# немецком), поэтому "Kind regards" — надёжный признак, если проверить, что рядом (в
# пределах ~400 символов) действительно встречается "MovetoRussia.com" — это отличает
# настоящий футер от случайного упоминания компании в тексте письма или личного
# "Kind regards, Имя" без прикреплённого фирменного блока.
SIGNATURE_ANCHOR_RE = re.compile(r"[\-\u2013\u2014]{1,3}[\s\xa0]*Kind\s+regards", re.IGNORECASE)


SIGNATURE_EMAIL_RE = re.compile(r"\S+@arkvostok\.com", re.IGNORECASE)
SIGNATURE_WINDOW = 400  # сколько символов после якоря считаем "телом" блока подписи


def strip_signature(body: str, role: str) -> tuple[str, Optional[str]]:
    """Вырезает повторяющийся рекламный блок подписи менеджера
    (--  Kind regards, Имя, Client relationship manager, MovetoRussia.com, email).

    Применяется ТОЛЬКО к письмам менеджера (role == manager) — у клиентов такого
    футера в принципе нет, а собственное "Kind regards, Имя" клиента резать нельзя.
    Режем строго от найденного якоря "-- Kind regards" и до email менеджера
    (@arkvostok.com) внутри блока подписи — если после подписи в письме есть ещё
    реальный текст (бывает, что подпись оказывается не в самом конце письма),
    он сохраняется, а не выбрасывается вместе с футером.
    """
    if role != ROLE_MANAGER:
        return body, None

    matches = list(SIGNATURE_ANCHOR_RE.finditer(body))
    if not matches:
        return body, None

    last = matches[-1]
    window_end = min(len(body), last.start() + SIGNATURE_WINDOW)
    window = body[last.start(): window_end]
    if SIGNATURE_MARKER.lower() not in window.lower():
        # "Kind regards" встретился, но рядом нет "MovetoRussia.com" — это не
        # фирменный футер, а просто вежливая фраза менеджера; не режем.
        return body, None

    # Конец собственно блока подписи — email менеджера внутри окна поиска.
    # Если email не нашёлся (бывают подписи без явного email) — берём границу окна.
    email_matches = list(SIGNATURE_EMAIL_RE.finditer(window))
    sig_end = last.start() + email_matches[-1].end() if email_matches else window_end

    head = body[: last.start()].rstrip()
    removed = body[last.start(): sig_end].strip()
    tail_after_sig = body[sig_end:].strip()

    if not head and not tail_after_sig:
        return body, None

    cleaned = head if not tail_after_sig else (f"{head}\n\n{tail_after_sig}".strip() if head else tail_after_sig)
    return cleaned, removed


WHITESPACE_RUN_RE = re.compile(r"[ \t]{2,}")
LEADING_SPACE_RE = re.compile(r"(?m)^[ \t]+")
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_whitespace(body: str) -> str:
    text = unicodedata.normalize("NFC", body)
    text = WHITESPACE_RUN_RE.sub(" ", text)
    text = LEADING_SPACE_RE.sub("", text)
    text = MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


@dataclass
class CleaningStats:
    quote_tails_removed: int = 0
    signatures_removed: int = 0
    quote_tail_samples: list = field(default_factory=list)
    signature_samples: list = field(default_factory=list)
    parse_warnings: list = field(default_factory=list)

    def add_quote_sample(self, thread_id: str, removed: str):
        self.quote_tails_removed += 1
        if len(self.quote_tail_samples) < 40:
            self.quote_tail_samples.append(f"[{thread_id}] {removed!r}")

    def add_signature_sample(self, thread_id: str, removed: str):
        self.signatures_removed += 1
        if len(self.signature_samples) < 20:
            snippet = removed.replace("\n", " ⏎ ")[:160]
            self.signature_samples.append(f"[{thread_id}] {snippet!r}")


def clean_body(raw_body: str, thread_id: str, role: str, stats: CleaningStats) -> str:
    body = raw_body.strip("\n")
    body, removed_quote = strip_quote_tail(body)
    if removed_quote:
        stats.add_quote_sample(thread_id, removed_quote)
    body, removed_sig = strip_signature(body, role)
    if removed_sig:
        stats.add_signature_sample(thread_id, removed_sig)
    body = normalize_whitespace(body)
    return body


# ==========================================================================
# Парсинг одного файла-треда
# ==========================================================================

@dataclass
class Message:
    index: int
    role: str               # "manager" | "client"
    role_email: str
    subject: str
    from_: str
    to: str
    date_raw: str
    date_iso: Optional[str]
    body: str
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.body.split())


def parse_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.isoformat()
    except (TypeError, ValueError):
        pass
    return None


def parse_thread_file(path: Path, stats: CleaningStats) -> Optional[dict]:
    raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    thread_id = path.stem

    parts = SEPARATOR_RE.split(raw_text)
    if len(parts) < 2:
        stats.parse_warnings.append(f"{path.name}: не найден ни один разделитель '===...', файл пропущен")
        return None

    head = parts[0]
    client_match = CLIENT_HEADER_RE.search(head)
    count_match = COUNT_HEADER_RE.search(head)
    client_email = client_match.group(1).strip() if client_match else None
    declared_count = int(count_match.group(1)) if count_match else None

    messages: list[Message] = []
    manager_emails: set[str] = set()

    for i, block in enumerate(parts[1:], start=1):
        block = block.strip("\n")
        if not block.strip():
            continue

        header, body_raw = parse_message_header(block)
        if header is None:
            stats.parse_warnings.append(
                f"{thread_id}: блок #{i} не распознан по формату заголовка, пропущен "
                f"(начало: {block[:80]!r})"
            )
            continue

        role = header["role"]
        body_clean = clean_body(body_raw, thread_id, role, stats)

        msg = Message(
            index=i,
            role=role,
            role_email=header["role_email"],
            subject=header["subject"],
            from_=header["from_"],
            to=header["to"],
            date_raw=header["date"],
            date_iso=parse_date(header["date"]),
            body=body_clean,
        )
        messages.append(msg)
        if role == ROLE_MANAGER and msg.role_email:
            manager_emails.add(msg.role_email)

    return {
        "thread_id": thread_id,
        "client_email": client_email,
        "declared_message_count": declared_count,
        "parsed_message_count": len(messages),
        "manager_emails": sorted(manager_emails),
        "messages": messages,
    }


# ==========================================================================
# Группировка писем в exchanges (смысловые единицы для эмбеддинга)
# ==========================================================================

LOW_SIGNAL_WORD_THRESHOLD = 12
INFORMATIVE_HINT_RE = re.compile(
    r"\d|виз|visa|paspor|паспорт|цена|price|eur|usd|rub|₽|€|\$|адрес|контракт|contract|"
    r"дата|date|документ|document|счет|счёт|invoice|residence|permit|address",
    re.IGNORECASE,
)


def build_exchanges(thread_id: str, client_email: Optional[str], messages: list[Message]) -> list[dict]:
    exchanges: list[dict] = []
    current_client: list[Message] = []
    current_manager: list[Message] = []

    def flush():
        if not current_client and not current_manager:
            return
        all_msgs = current_client + current_manager
        subject = next((m.subject for m in all_msgs if m.subject), "")
        dates = [m.date_iso for m in all_msgs if m.date_iso]

        text_parts = []
        for m in current_client:
            text_parts.append(f"КЛИЕНТ ({m.date_raw}): {m.body}")
        for m in current_manager:
            text_parts.append(f"МЕНЕДЖЕР ({m.date_raw}): {m.body}")
        text = "\n\n".join(p for p in text_parts if p.strip())

        word_count = len(text.split())
        low_signal = word_count < LOW_SIGNAL_WORD_THRESHOLD and not INFORMATIVE_HINT_RE.search(text)

        exchanges.append({
            "exchange_id": f"{thread_id}__ex{len(exchanges) + 1:03d}",
            "thread_id": thread_id,
            "client_email": client_email,
            "manager_emails": sorted({m.role_email for m in current_manager if m.role_email}),
            "subject": subject,
            "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None,
            "client_message_indices": [m.index for m in current_client],
            "manager_message_indices": [m.index for m in current_manager],
            "text": text,
            "word_count": word_count,
            "low_signal": low_signal,
        })

    for msg in messages:
        if msg.role == ROLE_CLIENT:
            if current_manager:
                # предыдущий обмен завершён ответом менеджера — начинаем новый
                flush()
                current_client, current_manager = [], []
            current_client.append(msg)
        elif msg.role == ROLE_MANAGER:
            current_manager.append(msg)
        else:
            # неизвестная роль — не теряем контент, прикрепляем как есть
            (current_manager if current_client else current_client).append(msg)

    flush()
    return exchanges


def detect_language(text: str) -> Optional[str]:
    if not HAVE_LANGDETECT or not text.strip():
        return None
    try:
        return _langdetect_detect(text[:1000])
    except Exception:
        return None


# ==========================================================================
# Опциональная LLM-дистилляция через DeepSeek
# ==========================================================================

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DISTILL_PROMPT = (
    "Ты помогаешь готовить базу знаний для RAG-ассистента менеджеров компании "
    "MoveToRussia (помощь с переездом в Россию, визы, недвижимость, ВНЖ).\n"
    "Ниже — фрагмент переписки менеджера с клиентом. Извлеки кратко:\n"
    "1) Суть вопроса/ситуации клиента.\n"
    "2) Конкретный ответ/факт/процедуру/цифры, которые дал менеджер.\n"
    "Игнорируй вежливые формулы и приветствия. Не придумывай ничего, чего нет в тексте. "
    "Ответ — 2-4 предложения на русском, без преамбулы."
)


def call_deepseek(text: str, api_key: str, model: str = "deepseek-v4-flash", retries: int = 3) -> Optional[str]:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": DISTILL_PROMPT},
            {"role": "user", "content": text[:6000]},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 429) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"    [distill] HTTP {e.code} на попытке {attempt}: {e.read()[:200]}", file=sys.stderr)
            return None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"    [distill] сетевая ошибка: {e}", file=sys.stderr)
            return None
    return None


# ==========================================================================
# Основной пайплайн
# ==========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Очистка и подготовка переписок MoveToRussia для RAG")
    ap.add_argument("--input", default="mailbox_export_clean/threads",
                     help="Папка с исходными .txt тредами (по умолчанию mailbox_export_clean/threads)")
    ap.add_argument("--output", default=None,
                     help="Папка для результата (по умолчанию — mailbox_export_RAG/threads, "
                          "рядом с input)")
    ap.add_argument("--distill", action="store_true",
                     help="Дополнительно прогнать каждый exchange через DeepSeek для сжатой "
                          "карточки знания (нужен DEEPSEEK_API_KEY)")
    ap.add_argument("--distill-model", default="deepseek-v4-flash")
    ap.add_argument("--distill-limit", type=int, default=None,
                     help="Ограничить число exchanges для дистилляции (для тестового прогона)")
    ap.add_argument("--min-exchange-words", type=int, default=0,
                     help="Не писать в corpus.jsonl exchanges короче N слов (0 = писать всё, "
                          "низкоинформативные просто помечаются флагом low_signal)")
    args = ap.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"ОШИБКА: входная папка не найдена: {input_dir.resolve()}", file=sys.stderr)
        return 1

    if args.output:
        output_threads_dir = Path(args.output)
    else:
        output_threads_dir = Path(str(input_dir).replace("mailbox_export_clean", "mailbox_export_RAG"))
        if str(output_threads_dir) == str(input_dir):
            # на случай нестандартного имени входной папки — просто соседняя mailbox_export_RAG
            output_threads_dir = input_dir.parent.parent / "mailbox_export_RAG" / "threads"

    output_root = output_threads_dir.parent
    output_threads_dir.mkdir(parents=True, exist_ok=True)

    print("=== MoveToRussia RAG corpus preparation ===")
    print(f"  Вход:  {input_dir.resolve()}")
    print(f"  Выход: {output_threads_dir.resolve()}")
    if args.distill:
        print(f"  Дистилляция: включена, модель={args.distill_model}")

    api_key = os.environ.get("DEEPSEEK_API_KEY") if args.distill else None
    if args.distill and not api_key:
        print("ОШИБКА: --distill указан, но переменная окружения DEEPSEEK_API_KEY не задана.", file=sys.stderr)
        return 1

    txt_files = sorted(input_dir.glob("*.txt"))
    if not txt_files:
        print(f"ОШИБКА: в {input_dir} не найдено .txt файлов", file=sys.stderr)
        return 1

    stats = CleaningStats()
    corpus_path = output_root / "corpus.jsonl"
    corpus_records = 0
    total_threads = 0
    total_messages = 0
    total_exchanges = 0
    total_low_signal = 0
    distilled_count = 0

    with corpus_path.open("w", encoding="utf-8") as corpus_f:
        for path in txt_files:
            parsed = parse_thread_file(path, stats)
            if parsed is None:
                continue

            total_threads += 1
            messages: list[Message] = parsed["messages"]
            total_messages += len(messages)

            exchanges = build_exchanges(parsed["thread_id"], parsed["client_email"], messages)
            total_exchanges += len(exchanges)
            total_low_signal += sum(1 for e in exchanges if e["low_signal"])

            for ex in exchanges:
                ex["language"] = detect_language(ex["text"])
                if args.distill and (args.distill_limit is None or distilled_count < args.distill_limit):
                    if not ex["low_signal"]:
                        summary = call_deepseek(ex["text"], api_key, args.distill_model)
                        ex["distilled"] = summary
                        distilled_count += 1
                        if distilled_count % 25 == 0:
                            print(f"    ...дистиллировано {distilled_count} exchanges")
                    else:
                        ex["distilled"] = None
                else:
                    ex["distilled"] = None

            # ---- запись per-thread JSON (полное дерево писем + exchanges) ----
            thread_out = {
                "thread_id": parsed["thread_id"],
                "client_email": parsed["client_email"],
                "manager_emails": parsed["manager_emails"],
                "declared_message_count": parsed["declared_message_count"],
                "parsed_message_count": parsed["parsed_message_count"],
                "messages": [asdict(m) for m in messages],
                "exchanges": exchanges,
            }
            out_path = output_threads_dir / f"{parsed['thread_id']}.json"
            out_path.write_text(
                json.dumps(thread_out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # ---- запись плоских chunks в corpus.jsonl ----
            for ex in exchanges:
                if ex["word_count"] < args.min_exchange_words:
                    continue
                record = {
                    "id": ex["exchange_id"],
                    "thread_id": ex["thread_id"],
                    "client_email": ex["client_email"],
                    "manager_emails": ex["manager_emails"],
                    "subject": ex["subject"],
                    "date_start": ex["date_start"],
                    "date_end": ex["date_end"],
                    "language": ex["language"],
                    "low_signal": ex["low_signal"],
                    "word_count": ex["word_count"],
                    "text": ex["text"],
                    "distilled": ex.get("distilled"),
                    "source": "mailbox_thread",
                }
                corpus_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                corpus_records += 1

            print(f"  [{total_threads}/{len(txt_files)}] {path.name}: "
                  f"{len(messages)} писем -> {len(exchanges)} exchanges "
                  f"({sum(1 for e in exchanges if e['low_signal'])} low-signal)")

    # ---- отчёт ----
    report_lines = [
        "=== Отчёт по подготовке RAG-корпуса MoveToRussia ===",
        f"Дата запуска: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Обработано файлов-тредов:      {total_threads}",
        f"Всего писем распарсено:        {total_messages}",
        f"Всего exchanges (chunks):      {total_exchanges}",
        f"  из них low_signal:           {total_low_signal} "
        f"({total_low_signal / total_exchanges * 100:.1f}%)" if total_exchanges else "  из них low_signal: 0",
        f"Записей в corpus.jsonl:        {corpus_records}",
        "",
        f"Хвостов цитат вырезано:        {stats.quote_tails_removed}",
        f"Подписей менеджеров вырезано:  {stats.signatures_removed}",
        f"Предупреждений парсинга:       {len(stats.parse_warnings)}",
    ]
    if args.distill:
        report_lines.append(f"Дистиллировано через DeepSeek:  {distilled_count}")

    if stats.parse_warnings:
        report_lines += ["", "--- Предупреждения парсинга (проверьте вручную) ---"]
        report_lines += stats.parse_warnings[:100]

    if stats.quote_tail_samples:
        report_lines += ["", "--- Примеры вырезанных хвостов цитат (для проверки эвристики) ---"]
        report_lines += stats.quote_tail_samples

    if stats.signature_samples:
        report_lines += ["", "--- Примеры вырезанных подписей ---"]
        report_lines += stats.signature_samples

    report_path = output_root / "prepare_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    print()
    print("=== Готово ===")
    print(f"  Тредов:      {total_threads}")
    print(f"  Писем:       {total_messages}")
    print(f"  Exchanges:   {total_exchanges}  (low_signal: {total_low_signal})")
    print(f"  corpus.jsonl:      {corpus_path.resolve()}")
    print(f"  per-thread JSON:   {output_threads_dir.resolve()}")
    print(f"  отчёт:             {report_path.resolve()}")
    if stats.parse_warnings:
        print(f"  ⚠ {len(stats.parse_warnings)} предупреждений парсинга — см. prepare_report.txt")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
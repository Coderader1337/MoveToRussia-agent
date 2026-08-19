#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_telegram_corpus.py
============================

Готовит корпус Telegram-переписок MoveToRussia для RAG — аналог
prepare_rag_corpus.py, но под формат экспорта Telegram Desktop (result.json).

Вход: result.json (полный экспорт аккаунта — data['chats']['list'], у каждого
чата есть type: personal_chat / private_group / private_supergroup / bot_chat /
saved_messages, и messages: [...]).

Что делает:
  1. Берёт чаты нужных типов (по умолчанию personal_chat, private_group,
     private_supergroup — исключая bot_chat и saved_messages, это не переписка
     с клиентами).
  2. Автоматически определяет identity компании (from_id, который встречается
     в наибольшем числе разных чатов) — на его основе размечает роли
     manager/client по каждому сообщению.
  3. Чистит каждое сообщение:
       - "text" в экспорте Telegram может быть строкой или списком объектов
         (форматирование/ссылки/упоминания) — всё схлопывается в обычный текст;
       - сообщения только с медиа (фото/файл/голосовое) без подписи заменяются
         на короткий плейсхолдер (важно для связности диалога), стикеры и
         контакт-карточки (PII) — просто пропускаются;
       - служебные сообщения (type: service — кто-то создал группу и т.п.)
         отбрасываются.
  4. Группирует сообщения в exchanges: по чередованию ролей client/manager,
     ПЛЮС принудительный разрыв, если между соседними сообщениями большой
     разрыв по времени (--max-gap-hours, по умолчанию 48ч) — иначе长ие
     персональные чаты (по 500-900 сообщений за год) слипались бы в один
     гигантский чанк без тематической связности.
  5. Пишет результат в той же схеме полей, что и corpus.jsonl из
     prepare_rag_corpus.py (id, text, source, метаданные) — чтобы можно было
     индексировать оба корпуса одним и тем же пайплайном.

Выход:
  <output>/chats/<chat_id>_<имя>.json   — по чату: все сообщения + exchanges
  <output>/corpus_telegram.jsonl        — плоский список chunks для индексации
  <output>/prepare_telegram_report.txt  — отчёт по чистке

Использование:
    python prepare_telegram_corpus.py --input result.json
    python prepare_telegram_corpus.py --input result.json --output telegram_export_RAG
    python prepare_telegram_corpus.py --input result.json --max-gap-hours 72
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from langdetect import detect as _langdetect_detect  # type: ignore
    from langdetect import DetectorFactory as _LDFactory  # type: ignore

    _LDFactory.seed = 0
    HAVE_LANGDETECT = True
except ImportError:
    HAVE_LANGDETECT = False


ROLE_MANAGER = "manager"
ROLE_CLIENT = "client"

DEFAULT_CHAT_TYPES = ("personal_chat", "private_group", "private_supergroup")
EXCLUDED_CHAT_TYPES = ("bot_chat", "saved_messages")


# ==========================================================================
# Разбор текста и медиа-плейсхолдеров
# ==========================================================================

def flatten_text(text_field) -> str:
    """В экспорте Telegram 'text' — либо строка, либо список строк/объектов
    {'type':..., 'text':...} (форматирование, ссылки, упоминания и т.д.)."""
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        parts = []
        for item in text_field:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def media_placeholder(msg: dict) -> Optional[str]:
    media_type = msg.get("media_type")
    if media_type == "sticker":
        return None  # стикеры без текста никогда не несут смысла
    if "contact_information" in msg:
        return None  # контакт-карточка — чистый PII без пользы для RAG, пропускаем
    if media_type == "voice_message":
        return "[голосовое сообщение]"
    if media_type == "video_message":
        return "[видео-кружок]"
    if media_type == "video_file":
        return "[видео]"
    if "photo" in msg:
        return "[фото]"
    if "file" in msg:
        fname = msg.get("file_name") or msg.get("mime_type") or "файл"
        return f"[файл: {fname}]"
    return None


def get_message_body(msg: dict) -> Optional[str]:
    text = flatten_text(msg.get("text", "")).strip()
    if text:
        return unicodedata.normalize("NFC", text)
    return media_placeholder(msg)


# ==========================================================================
# Определение identity компании
# ==========================================================================

def detect_company_from_id(chats: list[dict], explicit: Optional[str] = None) -> tuple[str, str]:
    """Возвращает (from_id, from_name) компании. Если задано явно — используем
    его. Иначе ищем from_id, встречающийся в наибольшем числе РАЗНЫХ чатов
    (клиент фигурирует обычно в 1-2 чатах, аккаунт компании — почти во всех)."""
    if explicit:
        return explicit, "(указано явно)"

    chats_per_id: dict[str, set] = defaultdict(set)
    names_per_id: dict[str, Counter] = defaultdict(Counter)
    for c in chats:
        for m in c.get("messages", []):
            if m.get("type") != "message" or not m.get("from_id"):
                continue
            chats_per_id[m["from_id"]].add(c.get("id"))
            names_per_id[m["from_id"]][m.get("from") or ""] += 1

    if not chats_per_id:
        raise SystemExit("Не удалось определить identity компании — нет сообщений с from_id")

    best_id = max(chats_per_id, key=lambda k: len(chats_per_id[k]))
    best_name = names_per_id[best_id].most_common(1)[0][0]
    return best_id, best_name


# ==========================================================================
# Парсинг сообщений одного чата
# ==========================================================================

@dataclass
class TgMessage:
    tg_id: int
    index: int
    role: str
    author: str
    date_iso: Optional[str]
    date_unix: int
    reply_to: Optional[int]
    body: str
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.body.split())


@dataclass
class ParseStats:
    skipped_service: int = 0
    skipped_no_content: int = 0
    media_placeholders: int = 0
    chats_processed: int = 0
    chats_skipped_type: Counter = field(default_factory=Counter)


def parse_chat_messages(chat: dict, company_from_id: str, stats: ParseStats) -> list[TgMessage]:
    messages: list[TgMessage] = []
    idx = 0
    for m in chat.get("messages", []):
        if m.get("type") != "message":
            stats.skipped_service += 1
            continue

        body = get_message_body(m)
        if not body:
            stats.skipped_no_content += 1
            continue
        if not flatten_text(m.get("text", "")).strip():
            stats.media_placeholders += 1

        idx += 1
        role = ROLE_MANAGER if m.get("from_id") == company_from_id else ROLE_CLIENT
        messages.append(TgMessage(
            tg_id=m.get("id"),
            index=idx,
            role=role,
            author=m.get("from") or "unknown",
            date_iso=m.get("date"),
            date_unix=int(m.get("date_unixtime") or 0),
            reply_to=m.get("reply_to_message_id"),
            body=body,
        ))
    return messages


# ==========================================================================
# Группировка в exchanges (роль + разрыв по времени)
# ==========================================================================

LOW_SIGNAL_WORD_THRESHOLD = 12
INFORMATIVE_HINT_RE = re.compile(
    r"\d|виз|visa|paspor|паспорт|цена|price|eur|usd|rub|₽|€|\$|адрес|контракт|contract|"
    r"дата|date|документ|document|счет|счёт|invoice|residence|permit|address|снилс|"
    r"регистрац|registration",
    re.IGNORECASE,
)


def build_exchanges(chat_id: str, chat_name: str, chat_type: str,
                     messages: list[TgMessage], max_gap_seconds: int) -> list[dict]:
    exchanges: list[dict] = []
    current: list[TgMessage] = []
    last_unix: Optional[int] = None
    last_role: Optional[str] = None

    def flush():
        if not current:
            return
        ordered = sorted(current, key=lambda m: m.index)
        lines = []
        for m in ordered:
            speaker = "МЕНЕДЖЕР" if m.role == ROLE_MANAGER else f"КЛИЕНТ ({m.author})"
            lines.append(f"{speaker} [{m.date_iso}]: {m.body}")
        text = "\n".join(lines)

        # low_signal и word_count считаем по ЧИСТОМУ телу сообщений, без дат/имён
        # спикеров — иначе дата в тексте (содержит цифры) ложно считается
        # "информативным" признаком, и флаг low_signal никогда не срабатывает.
        body_only = "\n".join(m.body for m in ordered)
        word_count = len(body_only.split())
        low_signal = word_count < LOW_SIGNAL_WORD_THRESHOLD and not INFORMATIVE_HINT_RE.search(body_only)

        dates = [m.date_iso for m in ordered if m.date_iso]
        authors = sorted({m.author for m in ordered if m.role == ROLE_CLIENT})

        exchanges.append({
            "exchange_id": f"tg{chat_id}__ex{len(exchanges) + 1:03d}",
            "chat_id": str(chat_id),
            "chat_name": chat_name,
            "chat_type": chat_type,
            "client_authors": authors,
            "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None,
            "message_ids": [m.tg_id for m in ordered],
            "text": text,
            "word_count": word_count,
            "low_signal": low_signal,
        })
        current.clear()

    for m in messages:
        gap_exceeded = (
            last_unix is not None and m.date_unix
            and (m.date_unix - last_unix) > max_gap_seconds
        )
        # разрыв по времени ИЛИ (клиент написал после ответа менеджера) -> новый exchange
        role_switch_closes_exchange = (
            m.role == ROLE_CLIENT and last_role == ROLE_MANAGER
        )
        if gap_exceeded or role_switch_closes_exchange:
            flush()

        current.append(m)
        last_unix = m.date_unix or last_unix
        last_role = m.role

    flush()
    return exchanges


def detect_language(text: str) -> Optional[str]:
    if not HAVE_LANGDETECT or not text.strip():
        return None
    try:
        return _langdetect_detect(text[:1000])
    except Exception:
        return None


def safe_filename(name: str, chat_id) -> str:
    base = re.sub(r"[^\w\-]+", "_", (name or "chat").strip(), flags=re.UNICODE)
    base = base.strip("_") or "chat"
    return f"{chat_id}_{base[:60]}"


# ==========================================================================
# Основной пайплайн
# ==========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Подготовка Telegram-переписок MoveToRussia для RAG")
    ap.add_argument("--input", default="result.json", help="Путь к result.json")
    ap.add_argument("--output", default="telegram_export_RAG",
                     help="Папка для результата (по умолчанию telegram_export_RAG)")
    ap.add_argument("--chat-types", default=",".join(DEFAULT_CHAT_TYPES),
                     help=f"Через запятую, какие типы чатов брать (по умолчанию: {', '.join(DEFAULT_CHAT_TYPES)})")
    ap.add_argument("--company-from-id", default=None,
                     help="ID аккаунта компании (from_id). Если не указан — определяется автоматически.")
    ap.add_argument("--max-gap-hours", type=float, default=48.0,
                     help="Разрыв по времени между сообщениями, после которого начинается новый exchange")
    ap.add_argument("--min-exchange-words", type=int, default=0)
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ОШИБКА: файл не найден: {input_path.resolve()}", file=sys.stderr)
        return 1

    output_root = Path(args.output)
    chats_dir = output_root / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)

    wanted_types = {t.strip() for t in args.chat_types.split(",") if t.strip()}

    print("=== MoveToRussia Telegram corpus preparation ===")
    print(f"  Вход:  {input_path.resolve()}")
    print(f"  Выход: {output_root.resolve()}")
    print(f"  Типы чатов: {sorted(wanted_types)}")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    all_chats = data.get("chats", {}).get("list", [])
    relevant_chats = [c for c in all_chats if c.get("type") in wanted_types]
    excluded_counter = Counter(c.get("type") for c in all_chats if c.get("type") not in wanted_types)

    company_from_id, company_name = detect_company_from_id(relevant_chats, args.company_from_id)
    print(f"  Identity компании: from_id={company_from_id} ({company_name!r})")
    print(f"  Релевантных чатов: {len(relevant_chats)} (исключено: {dict(excluded_counter)})")

    max_gap_seconds = int(args.max_gap_hours * 3600)
    stats = ParseStats()
    corpus_path = output_root / "corpus_telegram.jsonl"

    total_exchanges = 0
    total_low_signal = 0
    total_messages_kept = 0
    corpus_records = 0

    with corpus_path.open("w", encoding="utf-8") as corpus_f:
        for chat in relevant_chats:
            chat_id = chat.get("id")
            chat_name = chat.get("name") or "unnamed"
            chat_type = chat.get("type")

            messages = parse_chat_messages(chat, company_from_id, stats)
            if not messages:
                continue

            stats.chats_processed += 1
            total_messages_kept += len(messages)

            exchanges = build_exchanges(chat_id, chat_name, chat_type, messages, max_gap_seconds)
            total_exchanges += len(exchanges)
            total_low_signal += sum(1 for e in exchanges if e["low_signal"])

            for ex in exchanges:
                ex["language"] = detect_language(ex["text"])

            chat_out = {
                "chat_id": str(chat_id),
                "chat_name": chat_name,
                "chat_type": chat_type,
                "message_count": len(messages),
                "messages": [m.__dict__ for m in messages],
                "exchanges": exchanges,
            }
            out_path = chats_dir / f"{safe_filename(chat_name, chat_id)}.json"
            out_path.write_text(json.dumps(chat_out, ensure_ascii=False, indent=2), encoding="utf-8")

            for ex in exchanges:
                if ex["word_count"] < args.min_exchange_words:
                    continue
                record = {
                    "id": ex["exchange_id"],
                    "thread_id": ex["chat_id"],
                    "client_email": ", ".join(ex["client_authors"]) or None,  # telegram-идентификатор, не email
                    "manager_emails": [],
                    "subject": ex["chat_name"],
                    "date_start": ex["date_start"],
                    "date_end": ex["date_end"],
                    "language": ex["language"],
                    "low_signal": ex["low_signal"],
                    "word_count": ex["word_count"],
                    "text": ex["text"],
                    "distilled": None,
                    "source": "telegram_chat",
                    "chat_type": ex["chat_type"],
                }
                corpus_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                corpus_records += 1

            print(f"  [{stats.chats_processed}] {chat_name!r} ({chat_type}): "
                  f"{len(messages)} сообщений -> {len(exchanges)} exchanges")

    report_lines = [
        "=== Отчёт по подготовке Telegram-корпуса MoveToRussia ===",
        "",
        f"Identity компании: from_id={company_from_id} ({company_name!r})",
        f"Обработано чатов:              {stats.chats_processed}",
        f"Исключено чатов (типы): {dict(excluded_counter)}",
        "",
        f"Сообщений учтено:               {total_messages_kept}",
        f"  из них — медиа-плейсхолдеры:  {stats.media_placeholders}",
        f"Пропущено service-сообщений:    {stats.skipped_service}",
        f"Пропущено без контента:         {stats.skipped_no_content} "
        f"(стикеры/контакт-карточки без подписи)",
        "",
        f"Всего exchanges (chunks):       {total_exchanges}",
        f"  из них low_signal:            {total_low_signal} "
        f"({total_low_signal / total_exchanges * 100:.1f}%)" if total_exchanges else "  из них low_signal: 0",
        f"Записей в corpus_telegram.jsonl: {corpus_records}",
    ]
    report_path = output_root / "prepare_telegram_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    print()
    print("=== Готово ===")
    print(f"  Чатов:       {stats.chats_processed}")
    print(f"  Exchanges:   {total_exchanges} (low_signal: {total_low_signal})")
    print(f"  corpus_telegram.jsonl: {corpus_path.resolve()}")
    print(f"  отчёт:                 {report_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
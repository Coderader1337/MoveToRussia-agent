"""Telegram bot: managers ask questions, get RAG answers grounded in past precedents.

Framework choice: aiogram (vs python-telegram-bot) -- chosen because it is
asyncio-native end to end (fits naturally with our async retrieval/LLM calls
without extra thread pools), has a lighter router-based API for a single
message-in/answer-out flow like this one, and was already available in this
environment.

Flow: manager sends any text message -> we run the RAG chain -> reply with the
answer followed by a short "Sources" list (thread_id / subject / date).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtr_rag.chain import ask  # noqa: E402
from mtr_rag.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mtr_rag_bot")

WELCOME_TEXT = (
    "Привет! Я ассистент по базе знаний MoveToRussia.\n"
    "Задайте вопрос по прецедентам из переписки с клиентами или по FAQ, "
    "и я отвечу на основе реальных случаев.\n\n"
    "Например: «Какая стоимость White Gloves пакета?» или "
    "«Что отвечали клиенту про апостиль документов?»"
)

HELP_TEXT = (
    "Просто напишите вопрос обычным текстом.\n"
    "/topk N — задать число прецедентов для поиска (по умолчанию "
    f"{settings.retrieval_top_k})\n"
    "/help — это сообщение"
)

router_top_k: dict[int, int] = {}


def format_sources(sources: list[dict]) -> str:
    if not sources:
        return "Источники не найдены."
    lines = ["\nИсточники:"]
    for s in sources:
        bits = [f"thread_id={s.get('thread_id')}"]
        if s.get("subject"):
            bits.append(f"«{s['subject']}»")
        if s.get("date_start"):
            bits.append(str(s["date_start"])[:10])
        lines.append("• " + " ".join(bits))
    return "\n".join(lines)


async def handle_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


async def handle_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def handle_topk(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используйте: /topk 8")
        return
    k = max(1, min(20, int(parts[1])))
    router_top_k[message.from_user.id] = k
    await message.answer(f"Ок, буду искать top-{k} прецедентов.")


async def handle_question(message: Message) -> None:
    question = (message.text or "").strip()
    if not question:
        return

    top_k = router_top_k.get(message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        result = await asyncio.to_thread(ask, question, top_k=top_k)
    except Exception as exc:  # Voyage/Qdrant/DeepSeek unavailable, etc.
        logger.exception("RAG chain failed for question: %s", question)
        await message.answer(
            "Не получилось получить ответ — сервис поиска или генерации сейчас "
            "недоступен. Попробуйте ещё раз через минуту.\n"
            f"(техническая причина: {type(exc).__name__})"
        )
        return

    reply = result.answer.strip() + "\n" + format_sources(result.sources)
    # Telegram message limit is 4096 chars.
    await message.answer(reply[:4096])


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_topk, Command("topk"))
    dp.message.register(handle_question, F.text)
    return dp


async def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see rag/.env.example)")
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    logger.info("Bot starting, collection=%s", settings.qdrant_collection)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

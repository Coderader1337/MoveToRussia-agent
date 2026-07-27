"""Telegram bot: managers paste a client email, RAG finds precedents, bot drafts a reply.

Flow: manager sends client email + optional instructions -> RAG retrieval ->
DeepSeek drafts client-facing email -> reply with draft + Sources list.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, TelegramObject

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtr_rag.chain import ask, warmup_prompt  # noqa: E402
from mtr_rag.config import settings  # noqa: E402
from mtr_rag.whitelist import is_user_allowed, load_allowed_user_ids  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mtr_rag_bot")

WELCOME_TEXT = (
    "Hi! I'm the MoveToRussia email drafting assistant.\n\n"
    "Paste the client's latest email and add any instructions — I'll draft "
    "a reply using precedents from past cases and the internal FAQ.\n\n"
    "Example:\n"
    "Draft a reply. Clarify that work starts only after the retainer is paid.\n\n"
    "Client email:\n"
    "\"Hello, I would like to learn how you can help me relocate to Russia...\""
)

HELP_TEXT = (
    "Paste the client's email and optional instructions (what to clarify, include, avoid).\n"
    "You'll get a draft reply + a Sources list (precedents used).\n\n"
    "/topk N — number of precedents to retrieve (default "
    f"{settings.retrieval_top_k})\n"
    "/help — this message"
)

ACCESS_DENIED_TEXT = (
    "Sorry, this bot is for authorized MovetoRussia team members only. "
    "If you need access, please contact your manager."
)

router_top_k: dict[int, int] = {}


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            if not is_user_allowed(event.from_user.id):
                logger.warning(
                    "Access denied for user_id=%s username=@%s",
                    event.from_user.id,
                    event.from_user.username,
                )
                await event.answer(ACCESS_DENIED_TEXT)
                return None
        return await handler(event, data)


def format_sources(sources: list[dict]) -> str:
    if not sources:
        return "No sources found."
    lines = ["\nSources:"]
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
        await message.answer("Usage: /topk 8")
        return
    k = max(1, min(20, int(parts[1])))
    router_top_k[message.from_user.id] = k
    await message.answer(f"OK, will retrieve top-{k} precedents.")


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
            "Could not get an answer — search or generation service is temporarily "
            "unavailable. Please try again in a minute.\n"
            f"(technical reason: {type(exc).__name__})"
        )
        return

    reply = result.answer.strip() + "\n\n" + format_sources(result.sources)
    await message.answer(reply[:4096])


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(WhitelistMiddleware())
    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_topk, Command("topk"))
    dp.message.register(handle_question, F.text)
    return dp


async def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see rag/.env.example)")
    allowed = load_allowed_user_ids()
    warmup_prompt()
    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher()
    logger.info(
        "Bot starting, collection=%s, whitelist=%d users",
        settings.qdrant_collection,
        len(allowed),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

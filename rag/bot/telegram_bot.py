"""Telegram bot: managers paste a client email, RAG finds precedents, bot drafts a reply.

Flow: manager sends client email + optional instructions -> RAG retrieval ->
DeepSeek drafts client-facing email -> reply with draft + Sources list ->
mandatory 1–10 rating -> row appended to usage_stats.csv.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtr_rag.chain import ask, warmup_prompt  # noqa: E402
from mtr_rag.config import settings  # noqa: E402
from mtr_rag.whitelist import load_allowed_user_ids  # noqa: E402

from bot.middleware import (  # noqa: E402
    RATE_CALLBACK_PREFIX,
    PendingRatingGateMiddleware,
    WhitelistMiddleware,
)
from bot.stats import append_usage_row  # noqa: E402
from bot.user_state import (  # noqa: E402
    PENDING_ANSWER_KEY,
    PENDING_QUESTION_KEY,
    PENDING_TIMESTAMP_KEY,
    BotStates,
    build_fsm_storage,
    get_top_k,
    set_top_k,
    utc_now_iso,
)

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

RATING_PROMPT_TEXT = (
    "How useful was this answer?\n"
    "Please rate it from 1 (not useful) to 10 (very useful)."
)

RATING_THANKS_TEXT = "Thank you! Your rating ({rate}/10) has been recorded."


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


def build_rating_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"{RATE_CALLBACK_PREFIX}{i}")
        for i in range(1, 11)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:5], buttons[5:]])


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
    set_top_k(message.from_user.id, k)
    await message.answer(f"OK, will retrieve top-{k} precedents.")


async def handle_question(message: Message, state: FSMContext) -> None:
    question = (message.text or "").strip()
    if not question:
        return

    top_k = get_top_k(message.from_user.id)
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

    answer_text = result.answer.strip()
    reply = answer_text + "\n\n" + format_sources(result.sources)
    await message.answer(reply[:4096])

    await state.set_state(BotStates.awaiting_rating)
    await state.update_data(
        {
            PENDING_QUESTION_KEY: question,
            PENDING_ANSWER_KEY: answer_text,
            PENDING_TIMESTAMP_KEY: utc_now_iso(),
        }
    )
    await message.answer(RATING_PROMPT_TEXT, reply_markup=build_rating_keyboard())


async def handle_rate(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    try:
        rate = int(callback.data.removeprefix(RATE_CALLBACK_PREFIX))
    except ValueError:
        await callback.answer("Invalid rating.", show_alert=True)
        return

    if not 1 <= rate <= 10:
        await callback.answer("Please choose a rating from 1 to 10.", show_alert=True)
        return

    data = await state.get_data()
    question = data.get(PENDING_QUESTION_KEY, "")
    answer = data.get(PENDING_ANSWER_KEY, "")
    timestamp = data.get(PENDING_TIMESTAMP_KEY, utc_now_iso())
    telegram_id = callback.from_user.id

    try:
        path = await asyncio.to_thread(
            append_usage_row,
            timestamp=timestamp,
            telegram_id=telegram_id,
            question=question,
            answer=answer,
            rate=rate,
        )
    except Exception:
        logger.exception("Failed to write usage stats for user_id=%s", telegram_id)
        await callback.answer(
            "Could not save your rating. Please try again.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.answer(f"Rated {rate}/10")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(RATING_THANKS_TEXT.format(rate=rate))
    logger.info(
        "Usage logged user_id=%s rate=%d path=%s",
        telegram_id,
        rate,
        path,
    )


def build_dispatcher() -> Dispatcher:
    storage = build_fsm_storage()
    dp = Dispatcher(storage=storage)
    dp.message.middleware(WhitelistMiddleware())
    dp.callback_query.middleware(WhitelistMiddleware())
    dp.message.middleware(PendingRatingGateMiddleware())
    dp.callback_query.middleware(PendingRatingGateMiddleware())

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_topk, Command("topk"))
    dp.message.register(handle_question, F.text)
    dp.callback_query.register(
        handle_rate,
        BotStates.awaiting_rating,
        F.data.startswith(RATE_CALLBACK_PREFIX),
    )
    return dp


async def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see rag/.env)")
    allowed = load_allowed_user_ids()
    warmup_prompt()
    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher()
    logger.info(
        "Bot starting, collection=%s, whitelist=%d users, stats=%s",
        settings.qdrant_collection,
        len(allowed),
        settings.stats_csv_path,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

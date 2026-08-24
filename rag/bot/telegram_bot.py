"""Telegram bot: managers ask factual questions or paste a client email for a draft reply.

Flow: manager sends a question OR client email + instructions -> RAG retrieval ->
DeepSeek answers or drafts client-facing email -> reply + Sources list ->
mandatory 1–10 rating -> row appended to usage_stats.csv.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtr_rag.chain import ask, warmup_prompt  # noqa: E402
from mtr_rag.config import settings  # noqa: E402
from mtr_rag.mail_writing_prompt import DRAFT_SECTION_MARKER  # noqa: E402
from mtr_rag.whitelist import load_allowed_user_ids  # noqa: E402

from bot.middleware import (  # noqa: E402
    RATE_CALLBACK_PREFIX,
    RESET_BUTTON_TEXT,
    PendingRatingGateMiddleware,
    WhitelistMiddleware,
)
from bot.stats import append_usage_row  # noqa: E402
from bot.user_state import (  # noqa: E402
    PENDING_ANSWER_KEY,
    PENDING_QUESTION_KEY,
    PENDING_TIMESTAMP_KEY,
    BotStates,
    append_history_turn,
    build_fsm_storage,
    get_history,
    get_top_k,
    reset_history,
    set_top_k,
    utc_now_iso,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mtr_rag_bot")

WELCOME_TEXT = (
    "Hi! I'm the MoveToRussia internal assistant.\n\n"
    "You can:\n"
    "1. Ask a factual question — I'll search precedents and the FAQ and answer directly.\n"
    "   Example: What is the White Gloves package price?\n\n"
    "2. Paste a client email and ask for a draft reply — I'll draft a send-ready "
    "response using precedents and communication principles.\n"
    "   Example:\n"
    "   Draft a reply. Clarify that work starts only after the retainer is paid.\n\n"
    "   Client email:\n"
    "   \"Hello, I would like to learn how you can help me relocate to Russia...\"\n\n"
    "I remember recent messages in our chat, so follow-up questions work naturally. "
    f"Tap {RESET_BUTTON_TEXT} (below the input) or send /reset to start a new topic."
)

HELP_TEXT = (
    "Ask a factual question OR paste the client's email with optional instructions.\n\n"
    "Factual question → direct answer from the knowledge base.\n"
    "Client email + draft request → brief analysis, client questions, facts from KB, "
    "and a draft reply + Sources list.\n\n"
    "I remember the last few messages of our conversation, so you can ask follow-ups "
    "(e.g. \"and what about the timeline?\") without repeating context.\n\n"
    "/topk N — number of precedents to retrieve (default "
    f"{settings.retrieval_top_k})\n"
    f"{RESET_BUTTON_TEXT} or /reset — forget the current conversation thread and "
    "start a new topic\n"
    "/help — this message"
)

RESET_TEXT = "OK, conversation memory cleared. Starting a new topic."

RATING_PROMPT_TEXT = (
    "How useful was this answer?\n"
    "Please rate it from 1 (not useful) to 10 (very useful)."
)

RATING_THANKS_TEXT = (
    "Thank you! Your rating ({rate}/10) has been recorded.\n\n"
    "You can send your next request whenever you're ready."
)

PROCESSING_TEXT = (
    "Working on your request — extracting questions, searching the knowledge base, "
    "and generating the answer. This usually takes 30–90 seconds."
)

ERROR_TEXT = (
    "Could not get an answer — search or generation service is temporarily "
    "unavailable. Please try again in a minute.\n"
    "(technical reason: {reason})"
)


def format_sources(sources: list[dict]) -> str:
    if not sources:
        return "No sources found."
    lines = ["\nSources:"]
    for s in sources:
        if s.get("source") == "yandex_disk":
            label = s.get("file_path") or s.get("subject") or s.get("thread_id")
            bits = [f"official_file={label}", "priority=highest"]
        else:
            bits = [f"thread_id={s.get('thread_id')}"]
            if s.get("subject"):
                bits.append(f"«{s['subject']}»")
            if s.get("date_start"):
                bits.append(str(s["date_start"])[:10])
        lines.append("• " + " ".join(bits))
    return "\n".join(lines)


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard — always visible next to the text input."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=RESET_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_rating_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"{RATE_CALLBACK_PREFIX}{i}")
        for i in range(1, 11)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:5], buttons[5:]])


def split_answer_blocks(answer_text: str) -> tuple[str, str]:
    """Split LLM output into analysis (part 1) and draft email block (part 2)."""
    idx = answer_text.find(DRAFT_SECTION_MARKER)
    if idx == -1:
        lowered = answer_text.lower()
        alt = DRAFT_SECTION_MARKER.lower()
        idx = lowered.find(alt)
    if idx == -1:
        return answer_text.strip(), ""
    return answer_text[:idx].strip(), answer_text[idx:].strip()


async def send_answer_in_two_messages(
    message: Message, answer_text: str, sources: list[dict]
) -> None:
    """Message 1: analysis; message 2: draft email + sources."""
    analysis, draft = split_answer_blocks(answer_text)
    sources_block = format_sources(sources)

    if not draft:
        await send_text_chunks(message, answer_text + "\n\n" + sources_block)
        return

    await send_text_chunks(message, analysis)
    await send_text_chunks(message, draft + "\n\n" + sources_block)


async def send_text_chunks(message: Message, text: str, *, chunk_size: int = 4096) -> None:
    """Send long bot replies in multiple Telegram messages."""
    remaining = text.strip()
    while remaining:
        if len(remaining) <= chunk_size:
            await message.answer(remaining)
            return
        split_at = remaining.rfind("\n\n", 0, chunk_size)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, chunk_size)
        if split_at <= 0:
            split_at = chunk_size
        await message.answer(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()


async def handle_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT, reply_markup=build_main_keyboard())


async def handle_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=build_main_keyboard())


async def handle_reset(message: Message, state: FSMContext) -> None:
    reset_history(message.from_user.id)
    await state.clear()
    await message.answer(RESET_TEXT, reply_markup=build_main_keyboard())


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

    user_id = message.from_user.id
    top_k = get_top_k(user_id)
    history = get_history(user_id)
    await message.bot.send_chat_action(message.chat.id, "typing")
    status_message = await message.answer(PROCESSING_TEXT)

    try:
        result = await asyncio.to_thread(ask, question, top_k=top_k, history=history)
    except Exception as exc:  # Voyage/Qdrant/DeepSeek unavailable, etc.
        logger.exception("RAG chain failed for question: %s", question[:200])
        await status_message.edit_text(ERROR_TEXT.format(reason=type(exc).__name__))
        return

    answer_text = result.answer.strip()
    if not answer_text:
        await status_message.edit_text(
            ERROR_TEXT.format(reason="empty model response")
        )
        return

    try:
        await status_message.delete()
    except Exception:
        logger.warning("Could not delete processing status message", exc_info=True)

    try:
        await send_answer_in_two_messages(message, answer_text, result.sources)
    except Exception:
        logger.exception("Failed to send RAG answer to user_id=%s", message.from_user.id)
        await message.answer(ERROR_TEXT.format(reason="TelegramSendError"))
        return

    # Запоминаем пару вопрос/ответ в истории треда независимо от оценки —
    # FSM-состояние ниже используется только для гейта "сначала оцени", а
    # память диалога должна переживать несколько раундов оценки подряд.
    append_history_turn(user_id, question, answer_text)

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
        await callback.message.answer(
            RATING_THANKS_TEXT.format(rate=rate),
            reply_markup=build_main_keyboard(),
        )
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
    dp.message.register(
        handle_reset,
        or_f(Command("reset"), F.text == RESET_BUTTON_TEXT),
    )
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

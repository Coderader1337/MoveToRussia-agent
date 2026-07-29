"""Bot middleware: whitelist and mandatory-rating gate."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from mtr_rag.whitelist import is_user_allowed

from .user_state import BotStates

logger = logging.getLogger(__name__)

ACCESS_DENIED_TEXT = (
    "Sorry, this bot is for authorized MovetoRussia team members only. "
    "If you need access, please contact your manager."
)

RATE_REMINDER_TEXT = (
    "Please rate the previous answer using the buttons above (1–10). "
    "You need to submit a rating before sending a new request."
)

RATE_CALLBACK_PREFIX = "rate:"


def _event_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user.id
    return None


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _event_user_id(event)
        if user_id is not None and not is_user_allowed(user_id):
            username = ""
            if isinstance(event, Message) and event.from_user:
                username = event.from_user.username or ""
            elif isinstance(event, CallbackQuery) and event.from_user:
                username = event.from_user.username or ""
            logger.warning(
                "Access denied for user_id=%s username=@%s",
                user_id,
                username,
            )
            if isinstance(event, Message):
                await event.answer(ACCESS_DENIED_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return None
        return await handler(event, data)


class PendingRatingGateMiddleware(BaseMiddleware):
    """Block all interaction until the user rates the last answer (1–10)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)

        current = await state.get_state()
        if current != BotStates.awaiting_rating.state:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data and event.data.startswith(
            RATE_CALLBACK_PREFIX
        ):
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(RATE_REMINDER_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer("Please rate the answer first (1–10).", show_alert=True)
        return None

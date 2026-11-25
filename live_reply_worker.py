"""Receive manual execution confirmations from the configured Telegram owner."""

import asyncio
from datetime import timedelta
from sqlite3 import OperationalError
from typing import Protocol

from telegram import Bot, Update
from telegram.constants import ChatType
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from live_telegram import TIMEOUTS, parse_chat_id, parse_reply_callback, reply_markup
from news_journal import emit

_POLL_TIMEOUT = 20
_POLL_TYPES = (Update.CALLBACK_QUERY, Update.MESSAGE)
_ACTION_RESULTS = {
    "entered": "Ручной вход отмечен.",
    "skipped": "Карточка пропущена.",
    "closed": "Ручное закрытие отмечено.",
    "unchanged": "Ответ уже учтён.",
    "invalid": "Эта карточка не найдена или больше недоступна.",
}


class ReplySlots(Protocol):
    def respond(self, signal_id: str, action: str, *, route: str, message_id: int) -> str: ...

    def state(self, signal_id: str, *, route: str) -> str | None: ...

    def skip_pending(self, route: str) -> str: ...


def _log(status: str) -> None:
    emit({"type": "telegram_reply", "status": status})


def _private_owner(chat, user, chat_id: int) -> bool:
    actual_chat_id, actual_user_id = getattr(chat, "id", None), getattr(user, "id", None)
    return (
        type(actual_chat_id) is int
        and type(actual_user_id) is int
        and actual_chat_id == actual_user_id == chat_id
        and getattr(chat, "type", None) == ChatType.PRIVATE
    )


def _retry_seconds(error: RetryAfter) -> float:
    delay = error.retry_after
    return max(1.0, delay.total_seconds() if isinstance(delay, timedelta) else delay)


async def check_reply_transport(bot: Bot) -> str:
    """Refuse to start polling over an existing webhook."""
    try:
        info = await bot.get_webhook_info(**TIMEOUTS)
    except InvalidToken:
        return "token_invalid"
    except (TelegramError, OSError):
        return "telegram_unavailable"
    if not isinstance(info.url, str):
        return "telegram_unavailable"
    return "webhook_configured" if info.url else "ready"


async def _answer(bot: Bot, query_id: str, text: str) -> None:
    try:
        await bot.answer_callback_query(callback_query_id=query_id, text=text, **TIMEOUTS)
    except (TelegramError, OSError):
        _log("acknowledgment_unavailable")


async def _edit_buttons(bot: Bot, chat_id: int, message_id: int, signal_id: str, state: str | None) -> None:
    markup = reply_markup(signal_id, state)
    for attempt in range(2):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=markup, **TIMEOUTS,
            )
            return
        except BadRequest as error:
            if str(error).casefold().startswith("message is not modified"):
                return
            break
        except RetryAfter as error:
            wait = _retry_seconds(error)
            if wait > 2:
                break
        except (NetworkError, OSError):
            wait = 0.25
        except TelegramError:
            break
        if not attempt:
            await asyncio.sleep(wait)
    _log("reply_markup_unavailable")


async def _callback(bot: Bot, query, chat_id: int, route: str, slots: ReplySlots,
                    wake: asyncio.Event, edit_lock: asyncio.Lock) -> None:
    message = query.message
    if message is None or not _private_owner(message.chat, query.from_user, chat_id):
        return
    identity = parse_reply_callback(query.data)
    message_id = message.message_id
    if identity is None or type(message_id) is not int or message_id <= 0:
        return

    signal_id, action = identity
    async with edit_lock:
        result = slots.respond(signal_id, action, route=route, message_id=message_id)
        if result in {"entered", "skipped", "closed"}:
            wake.set()
        if result in {"entered", "skipped", "closed", "unchanged"}:
            response_state = slots.state(signal_id, route=route)
            await _edit_buttons(bot, chat_id, message_id, signal_id, response_state)
    status = result if result in _ACTION_RESULTS else "invalid"
    _log(status)
    await _answer(bot, query.id, _ACTION_RESULTS[status])


async def _skip_pending(bot: Bot, chat_id: int, route: str, slots: ReplySlots, wake: asyncio.Event) -> None:
    if slots.skip_pending(route) == "skipped":
        wake.set()
        status, text = "pending_skipped", "Неподтверждённая доставка пропущена."
    else:
        status, text = "no_pending_delivery", "Нет неподтверждённой доставки для пропуска."
    _log(status)
    try:
        await bot.send_message(chat_id=chat_id, text=text, allow_paid_broadcast=False, **TIMEOUTS)
    except (TelegramError, OSError):
        _log("acknowledgment_unavailable")


async def run_replies(
    bot: Bot, chat_id: int, route: str, slots: ReplySlots, wake: asyncio.Event, *,
    edit_lock: asyncio.Lock | None = None,
) -> None:
    """Poll callbacks for the one configured owner without modifying webhook settings."""
    chat_id = parse_chat_id(chat_id)
    lock = edit_lock if edit_lock is not None else asyncio.Lock()
    offset = None
    delay = 1
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=_POLL_TIMEOUT, allowed_updates=_POLL_TYPES, **TIMEOUTS)
        except Conflict:
            _log("telegram_polling_conflict")
            raise RuntimeError("telegram_polling_conflict") from None
        except (InvalidToken, Forbidden, BadRequest):
            _log("telegram_polling_rejected")
            raise RuntimeError("telegram_polling_rejected") from None
        except RetryAfter as error:
            _log("telegram_polling_rate_limited")
            await asyncio.sleep(_retry_seconds(error))
            continue
        except (TelegramError, OSError):
            _log("telegram_polling_unavailable")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
            continue

        delay = 1
        for update in updates:
            try:
                if update.callback_query is not None:
                    await _callback(bot, update.callback_query, chat_id, route, slots, wake, lock)
                elif (
                    (message := update.message) is not None
                    and message.text == "/skip_pending"
                    and _private_owner(message.chat, message.from_user, chat_id)
                ):
                    await _skip_pending(bot, chat_id, route, slots, wake)
            except OperationalError:
                _log("telegram_reply_storage_unavailable")
                await asyncio.sleep(delay)
                break
            offset = update.update_id + 1

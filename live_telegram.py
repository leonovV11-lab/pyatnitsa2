"""Deliver verified live signals to a configured private Telegram chat."""

import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, TypedDict, TypeGuard
from urllib.parse import urlsplit

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ChatType
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    RetryAfter,
    TelegramError,
)

MOSCOW_TIME = timezone(timedelta(hours=3))
MAX_CHAT_ID = 2 ** 52 - 1
_REPLY_ID = r"[A-Za-z0-9_-]{1,59}"
_REPLY_ACTIONS = {"e": "enter", "s": "skip", "c": "close"}
UPDATE_TEXT = {
    "alert_expired": "ИСТЁК. Срок действия сигнала завершился.",
    "expired": "ИСТЁК. Срок действия сигнала завершился.",
    "source_unavailable": "ОТМЕНЁН. Официальный источник недоступен.",
    "source_revision_changed": "ОТМЕНЁН. Официальный источник изменил публикацию.",
    "source_no_longer_lists_release": "ОТМЕНЁН. Официальный источник удалил публикацию из ленты.",
}


class _Timeouts(TypedDict):
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float


class _ReplyMarkup(TypedDict, total=False):
    reply_markup: InlineKeyboardMarkup | None


TIMEOUTS: _Timeouts = {
    "connect_timeout": 2.0,
    "read_timeout": 4.0,
    "write_timeout": 4.0,
    "pool_timeout": 1.0,
}


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "rate_limited", "rejected", "unconfirmed"]
    message_id: int | None = None
    retry_after: int | None = None
    reason: str | None = None


def _is_positive_int(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0


def parse_chat_id(value: object) -> int:
    """Accept only an explicit positive Telegram user/chat ID."""
    if isinstance(value, str):
        digits = value.strip()
        if re.fullmatch(r"[0-9]{1,16}", digits):
            value = int(digits)
    if type(value) is not int or not 0 < value <= MAX_CHAT_ID:
        raise ValueError("Нужен числовой ID личного чата Telegram.")
    return value


def _chat_result(chat, chat_id: int) -> str:
    actual_id = getattr(chat, "id", None)
    if not _is_positive_int(actual_id) or actual_id != chat_id:
        return "chat_mismatch"
    if getattr(chat, "type", None) != ChatType.PRIVATE:
        return "chat_not_private"
    return "ready"


def _delivery_error(error: TelegramError | OSError) -> DeliveryResult:
    if isinstance(error, RetryAfter):
        delay = error.retry_after
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
        return DeliveryResult("rate_limited", retry_after=max(1, math.ceil(seconds)), reason="rate_limited")
    if isinstance(error, InvalidToken):
        return DeliveryResult("rejected", reason="token_invalid")
    if isinstance(error, ChatMigrated):
        return DeliveryResult("rejected", reason="chat_mismatch")
    if isinstance(error, (BadRequest, Forbidden)):
        return DeliveryResult("rejected", reason="telegram_rejected")
    return DeliveryResult("unconfirmed", reason="telegram_unavailable")


async def verify_chat(bot: Bot, chat_id: int) -> str:
    """Verify the destination before the application activates delivery."""
    try:
        chat_id = parse_chat_id(chat_id)
    except ValueError:
        return "chat_id_invalid"
    try:
        chat = await bot.get_chat(chat_id=chat_id, **TIMEOUTS)
    except (TelegramError, OSError) as error:
        return _delivery_error(error).reason or "telegram_unavailable"
    return _chat_result(chat, chat_id)


def _positive(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid number")
    return value


def _cost_rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
        raise ValueError("Invalid cost rate")
    return value


def _decimal_text(value: float, significant_digits: int | None = None) -> str:
    digits = str(value) if significant_digits is None else format(value, f".{significant_digits}g")
    return format(Decimal(digits), "f")


def _headline(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Invalid headline")
    clean = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cs"} else char
        for char in value
        if unicodedata.category(char) != "Cf"
    )
    clean = " ".join(clean.split())
    if not clean:
        raise ValueError("Empty headline")
    return clean if len(clean) <= 280 else f"{clean[:279]}…"


def _url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024:
        raise ValueError("Invalid URL length")
    if any(char.isspace() or char in '<>"\\' or unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Invalid URL characters")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme != "https" or not host or "." not in host
        or re.fullmatch(r"[a-z0-9.-]+", host) is None
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Invalid HTTPS URL")
    return value


def _label(value: object, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("Invalid signal label")
    return value


def parse_reply_callback(data: object) -> tuple[str, str] | None:
    """Read the callback identifier and action before validating the stored message."""
    if not isinstance(data, str):
        return None
    match = re.fullmatch(rf"lq:([esc]):({_REPLY_ID})", data)
    if match is None:
        return None
    code, signal_id = match.groups()
    return signal_id, _REPLY_ACTIONS[code]


def reply_markup(signal_id: str, response_state: str | None) -> InlineKeyboardMarkup | None:
    """Make the buttons valid for the current persisted response state."""
    signal_id = _label(signal_id, _REPLY_ID)
    if response_state == "waiting":
        buttons = [
            InlineKeyboardButton("Вошёл", callback_data=f"lq:e:{signal_id}"),
            InlineKeyboardButton("Пропуск", callback_data=f"lq:s:{signal_id}"),
        ]
    elif response_state == "entered":
        buttons = [InlineKeyboardButton("Закрыл", callback_data=f"lq:c:{signal_id}")]
    else:
        return None
    return InlineKeyboardMarkup([buttons])


def _evidence_lines(evidence: object) -> list[str]:
    if not isinstance(evidence, dict):
        return []
    lines = []
    for field, label in (("reaction_pct", "Реакция инструмента"), ("btc_reaction_pct", "Реакция BTC")):
        value = evidence.get(field)
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
            lines.append(f"{label}: {value:+.2f}%")
    agents = evidence.get("completed_agents")
    if _is_positive_int(agents):
        lines.append(f"Проверку завершили {agents} агентов.")
    return lines


def _render(signal: dict, reason: str | None = None) -> tuple[str, float]:
    technical = signal.get("type") == "technical_setup"
    venue = _label(signal.get("venue"), r"[a-zA-Z0-9_-]{1,32}")
    symbol = _label(signal.get("symbol"), r"[A-Z0-9]{1,24}/[A-Z0-9]{1,24}")
    signal_id = _label(signal.get("id"), r"[A-Za-z0-9_-]{1,64}")
    if signal.get("side") != "buy" or not isinstance(plan := signal.get("plan"), dict):
        raise ValueError("Invalid plan")
    asset, quote_asset = symbol.split("/")
    if quote_asset != plan.get("quote_asset", "USDT") or asset == quote_asset:
        raise ValueError("Invalid quote asset")
    headline = _headline(signal.get("headline"))
    source_url = _url(signal.get("url"))
    expiry = _positive(signal.get("expires_at"))
    local_expiry = datetime.fromtimestamp(expiry, tz=MOSCOW_TIME).strftime("%d.%m.%Y %H:%M:%S")
    entry, stop, tp1, tp2 = (_positive(plan.get(key)) for key in ("price", "stop", "tp1", "tp2"))
    if not stop < entry < tp1 <= tp2:
        raise ValueError("Invalid price order")
    market_label = f"{symbol} · {venue.upper()}"
    if technical:
        market_label = f"Технический сигнал · {market_label}"
    if reason is None:
        title = market_label if technical else f"Покупка на споте · {market_label}"
        lines = [f"Пятница 2 · {title}"]
    else:
        status = UPDATE_TEXT.get(reason, "ОТМЕНЁН. Сигнал больше не действует.")
        lines = [
            f"Пятница 2 · {market_label}",
            f"Статус: {status}",
            "Историческая котировка и параметры на момент публикации.",
        ]
    lines += ["", f"Вход по снимку рынка: {_decimal_text(entry)} {quote_asset}"]
    if technical or "entry_min" in plan or "entry_max" in plan:
        entry_min = _positive(plan.get("entry_min"))
        entry_max = _positive(plan.get("entry_max"))
        if not entry_min <= entry <= entry_max <= tp1 or (technical and entry_max != entry):
            raise ValueError("Invalid entry range")
        lines.append(f"Диапазон входа: {_decimal_text(entry_min)}–{_decimal_text(entry_max)} {quote_asset}")
    lines += [
        f"Стоп: {_decimal_text(stop)} {quote_asset}",
        f"Цель 1: {_decimal_text(tp1)} {quote_asset} · Цель 2: {_decimal_text(tp2)} {quote_asset}",
    ]
    if technical:
        stressed_ratio = _positive(plan.get("stressed_ratio"))
        fee_rate = _cost_rate(plan.get("fee_rate"))
        extra_cost = _cost_rate(plan.get("extra_cost_per_side"))
        if fee_rate + extra_cost >= 1:
            raise ValueError("Invalid combined cost rate")
        quote_stamp = datetime.fromtimestamp(_positive(plan.get("quote_observed_at")), tz=MOSCOW_TIME)
        lines += [
            f"Снимок стакана в {quote_stamp:%H:%M:%S} МСК.",
            f"В расчёте комиссия {fee_rate:.2%} и запас на издержки {extra_cost:.2%} на сторону.",
            f"При обеих целях с повышенными издержками · {_decimal_text(stressed_ratio, 4)} R",
            "Стоп и цели описывают расчётный сценарий.",
            "Объём позиции и фактическая доходность требуют данных о портфеле и исполнениях.",
        ]
    else:
        qty, loss, reward, ratio = (_positive(plan.get(key)) for key in ("qty", "loss", "reward", "ratio"))
        stress = _positive(plan.get("stressed_loss_usdt", loss))
        if stress < loss:
            raise ValueError("Invalid stressed risk")
        lines += [
            f"Объём: {_decimal_text(qty)} {asset}",
            f"Риск по плану: {_decimal_text(loss, 6)} USDT · При росте издержек: {_decimal_text(stress, 6)} USDT",
            f"Расчёт при обеих целях: {_decimal_text(reward, 6)} USDT · {_decimal_text(ratio, 4)} R",
        ]
        if "stressed_ratio" in plan or "stressed_reward_usdt" in plan:
            stressed_ratio = _positive(plan.get("stressed_ratio"))
            stressed_reward = _positive(plan.get("stressed_reward_usdt"))
            lines.append(
                f"При обеих целях с повышенными издержками · {_decimal_text(stressed_reward, 6)} USDT"
                f" · {_decimal_text(stressed_ratio, 4)} R"
            )
    if evidence := _evidence_lines(signal.get("evidence")):
        lines += ["", *evidence]
    if reason is None:
        lines += [
            "", f"Действует до {local_expiry} МСК.",
            "Если цена изменилась или срок истёк, котировка историческая и сигнал устарел.",
        ]
    else:
        lines += ["", f"Срок действия сигнала: до {local_expiry} МСК."]
    if not technical and signal.get("reply_required") is True:
        lines += ["", "«Вошёл» подтверждает уже совершённый вручную вход. Кнопки не продлевают срок сигнала."]
    lines += ["", headline, source_url, "", f"Пятница 2 · ID {signal_id}"]
    text = "\n".join(lines)
    if len(text.encode("utf-16-le")) // 2 > 4_096:
        raise ValueError("Signal too long")
    return text, expiry


def _message_result(message, chat_id: int, expected_id: int | None = None) -> DeliveryResult:
    message_id = getattr(message, "message_id", None)
    if (
        _chat_result(getattr(message, "chat", None), chat_id) == "ready"
        and _is_positive_int(message_id)
        and (expected_id is None or message_id == expected_id)
    ):
        return DeliveryResult("sent", message_id=message_id)
    return DeliveryResult("unconfirmed", reason="invalid_reply")


async def send_signal(bot: Bot, chat_id: int, signal: dict) -> DeliveryResult:
    """Deliver an unexpired signal to a chat accepted by verify_chat."""
    try:
        chat_id = parse_chat_id(chat_id)
    except ValueError:
        return DeliveryResult("rejected", reason="chat_id_invalid")
    try:
        text, expiry = _render(signal)
        markup = (reply_markup(signal["id"], "waiting")
                  if signal.get("type") != "technical_setup" and signal.get("reply_required") is True else None)
    except (AttributeError, OverflowError, OSError, TypeError, ValueError):
        return DeliveryResult("rejected", reason="signal_invalid")
    if expiry <= time.time():
        return DeliveryResult("rejected", reason="signal_expired")
    try:
        message = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True),
            allow_paid_broadcast=False, reply_markup=markup,
            **TIMEOUTS,
        )
    except (TelegramError, OSError) as error:
        return _delivery_error(error)
    return _message_result(message, chat_id)


async def edit_signal(
    bot: Bot, chat_id: int, message_id: int, signal: dict, reason: str, *, response_state: str | None = "waiting",
) -> DeliveryResult:
    """Amend the original signal after a source change or expiry."""
    try:
        chat_id = parse_chat_id(chat_id)
    except ValueError:
        return DeliveryResult("rejected", reason="chat_id_invalid")
    if not _is_positive_int(message_id):
        return DeliveryResult("rejected", reason="message_id_invalid")
    try:
        text, _ = _render(signal, reason)
        markup: _ReplyMarkup = {}
        if signal.get("type") == "technical_setup":
            markup["reply_markup"] = None
        elif signal.get("reply_required") is True:
            markup["reply_markup"] = reply_markup(signal["id"], response_state)
    except (AttributeError, OverflowError, OSError, TypeError, ValueError):
        return DeliveryResult("rejected", reason="signal_invalid")
    try:
        message = await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True), **markup, **TIMEOUTS,
        )
    except (TelegramError, OSError) as error:
        if isinstance(error, BadRequest) and str(error).casefold().startswith("message is not modified"):
            # The requested update already matches the original message.
            return DeliveryResult("sent", message_id=message_id)
        return _delivery_error(error)
    return _message_result(message, chat_id, expected_id=message_id)

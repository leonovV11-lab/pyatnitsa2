import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Bot
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TimedOut

from live_telegram import (
    DeliveryResult,
    edit_signal,
    parse_chat_id,
    parse_reply_callback,
    send_signal,
    verify_chat,
)

CHAT_ID = 1_234_567_890
NOW = datetime(2026, 11, 25, 11, 34, tzinfo=UTC).timestamp()


def chat(chat_id=CHAT_ID, chat_type=ChatType.PRIVATE):
    return SimpleNamespace(id=chat_id, type=chat_type)


def signal():
    return {
        "id": "signal-01",
        "headline": "Решение <ФРС> *сегодня*\nпо ставке\u202e",
        "url": "https://example.org/release?x=1&y=2",
        "venue": "okx",
        "symbol": "BTC/USDT",
        "side": "buy",
        "expires_at": NOW + 4,
        "evidence": {"reaction_pct": 2.345, "btc_reaction_pct": -0.78, "completed_agents": 50},
        "plan": {
            "entry_min": 12500,
            "entry_max": 12650.75,
            "price": 12550.5,
            "stop": 12450.25,
            "tp1": 12650.75,
            "tp2": 12800,
            "qty": 0.0008,
            "loss": 0.11,
            "reward": 0.18,
            "ratio": 1.63636,
            "stressed_loss_usdt": 0.13,
        },
    }


class LiveTelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = AsyncMock(spec=Bot)
        self.bot.get_chat.return_value = chat()
        self.bot.send_message.return_value = SimpleNamespace(message_id=35, chat=chat())
        self.bot.edit_message_text.return_value = SimpleNamespace(message_id=35, chat=chat())
        self.clock = patch("live_telegram.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_parses_only_positive_private_ids(self):
        self.assertEqual(parse_chat_id(CHAT_ID), CHAT_ID)
        self.assertEqual(parse_chat_id(f"  {CHAT_ID} "), CHAT_ID)
        for invalid in ("", "0", "-1", "1e2", "1.5", "+123", "@name", "１２３", "9" * 100, 2 ** 52, True, 1.0, None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "Telegram"):
                parse_chat_id(invalid)

    async def test_verifies_exact_private_chat_with_timeouts(self):
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "ready")
        kwargs = self.bot.get_chat.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHAT_ID)
        self.assertGreater(kwargs["connect_timeout"], 0)
        self.assertGreater(kwargs["read_timeout"], 0)
        self.bot.get_chat.return_value = chat(CHAT_ID + 1)
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "chat_mismatch")
        self.bot.get_chat.return_value = chat(chat_type=ChatType.SUPERGROUP)
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "chat_not_private")
        self.bot.get_chat.reset_mock()
        self.assertEqual(await verify_chat(self.bot, -CHAT_ID), "chat_id_invalid")
        self.bot.get_chat.assert_not_awaited()

    async def test_verification_sanitizes_error(self):
        self.bot.get_chat.side_effect = InvalidToken("123:secret")
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "token_invalid")
        self.bot.get_chat.side_effect = Forbidden("blocked 123:secret")
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "telegram_rejected")
        self.bot.get_chat.side_effect = TimedOut("token=123:secret")
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "telegram_unavailable")
        self.bot.get_chat.side_effect = RetryAfter(18)
        self.assertEqual(await verify_chat(self.bot, CHAT_ID), "rate_limited")

    async def test_send_formats_plain_text_plan_and_expires_in_moscow_time(self):
        result = await send_signal(self.bot, CHAT_ID, signal())
        self.assertEqual(result, DeliveryResult(status="sent", message_id=35))
        kwargs = self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHAT_ID)
        self.assertIsNone(kwargs["parse_mode"])
        self.assertFalse(kwargs["allow_paid_broadcast"])
        self.assertTrue(kwargs["link_preview_options"].is_disabled)
        self.assertGreater(kwargs["connect_timeout"], 0)
        self.assertGreater(kwargs["read_timeout"], 0)

        text = kwargs["text"]
        for expected in (
            "Пятница 2", "BTC/USDT", "OKX", "12550.5", "12450.25", "12650.75", "12800",
            "0.0008", "0.13", "25.11.2026 14:34:04 МСК", "Решение <ФРС> *сегодня* по ставке",
            "12500–12650.75", "+2.35%", "-0.78%", "50 агентов",
            "https://example.org/release?x=1&y=2", "signal-01", "цена изменилась",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("\u202e", text)

    async def test_stale_signal_and_bad_fields_never_touch_transport(self):
        stale = signal()
        stale["expires_at"] = NOW
        self.assertEqual((await send_signal(self.bot, CHAT_ID, stale)).reason, "signal_expired")
        bad_plan = signal()
        bad_plan["plan"]["stop"] = float("nan")
        self.assertEqual((await send_signal(self.bot, CHAT_ID, bad_plan)).reason, "signal_invalid")
        for url in ("http://example.org", "https://u@example.org", "https://bad host.org", "https://example.org/\nfoo"):
            bad_url = {**signal(), "url": url}
            self.assertEqual((await send_signal(self.bot, CHAT_ID, bad_url)).reason, "signal_invalid")
        self.assertEqual((await send_signal(self.bot, -CHAT_ID, signal())).reason, "chat_id_invalid")
        self.bot.send_message.assert_not_awaited()

    async def test_send_maps_unknown_delivery_without_exposing_errors(self):
        self.bot.send_message.side_effect = TimedOut("request https://api.telegram.org/bot123:secret")
        self.assertEqual(await send_signal(self.bot, CHAT_ID, signal()),
                         DeliveryResult(status="unconfirmed", reason="telegram_unavailable"))
        self.bot.send_message.side_effect = BadRequest("token 123:secret")
        self.assertEqual(await send_signal(self.bot, CHAT_ID, signal()),
                         DeliveryResult(status="rejected", reason="telegram_rejected"))
        self.bot.send_message.side_effect = RetryAfter(timedelta(seconds=17))
        self.assertEqual(await send_signal(self.bot, CHAT_ID, signal()),
                         DeliveryResult(status="rate_limited", retry_after=17, reason="rate_limited"))

    async def test_keeps_tiny_price_precision_and_shortens_large_cash_values(self):
        alert = signal()
        alert["symbol"] = "PEPE/USDT"
        alert["plan"].update(
            entry_min=0.00000037, entry_max=0.00000039, price=0.000000381223,
            stop=0.000000368, tp1=0.0000004, tp2=0.00000045,
            loss=0.03123456789, reward=0.2123456789,
        )
        self.assertEqual((await send_signal(self.bot, CHAT_ID, alert)).status, "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("0.000000381223", text)
        self.assertIn("0.0312346 USDT", text)
        self.assertIn("0.212346 USDT", text)

    async def test_invalid_reply_is_an_unconfirmed_send(self):
        self.bot.send_message.return_value = SimpleNamespace(message_id=35, chat=chat(CHAT_ID + 1))
        self.assertEqual((await send_signal(self.bot, CHAT_ID, signal())).status, "unconfirmed")
        self.bot.send_message.return_value = SimpleNamespace(message_id=35, chat=chat(chat_type=ChatType.GROUP))
        self.assertEqual((await send_signal(self.bot, CHAT_ID, signal())).status, "unconfirmed")
        self.bot.send_message.return_value = SimpleNamespace(message_id=True, chat=chat())
        self.assertEqual((await send_signal(self.bot, CHAT_ID, signal())).status, "unconfirmed")

    async def test_edit_appends_safe_cancellation_or_expiry_on_original_message(self):
        original = deepcopy(signal())
        for reason, line in (("source_revision_changed", "ОТМЕНЁН"), ("alert_expired", "ИСТЁК")):
            received = await edit_signal(self.bot, CHAT_ID, 35, original, reason)
            self.assertEqual(received, DeliveryResult(status="sent", message_id=35))
            kwargs = self.bot.edit_message_text.await_args.kwargs
            self.assertEqual(kwargs["message_id"], 35)
            self.assertEqual(kwargs["chat_id"], CHAT_ID)
            self.assertTrue(kwargs["link_preview_options"].is_disabled)
            header, status, *_ = kwargs["text"].splitlines()
            self.assertIn(line, status)
            self.assertNotIn("Покупка", header)
            self.assertIn("историческая котировка", kwargs["text"].lower())
            self.assertNotIn("reply_markup", kwargs)
        self.assertEqual(original, signal())
        unknown = "raw secret error 123:secret"
        await edit_signal(self.bot, CHAT_ID, 35, original, unknown)
        self.assertNotIn(unknown, self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_repeated_identical_edit_is_success_and_mismatched_edit_is_unconfirmed(self):
        self.bot.edit_message_text.side_effect = BadRequest("Message is not modified: same content")
        self.assertEqual(await edit_signal(self.bot, CHAT_ID, 35, signal(), "alert_expired"),
                         DeliveryResult(status="sent", message_id=35))
        self.bot.edit_message_text.side_effect = None
        self.bot.edit_message_text.return_value = SimpleNamespace(message_id=36, chat=chat())
        self.assertEqual((await edit_signal(self.bot, CHAT_ID, 35, signal(), "alert_expired")).status,
                         "unconfirmed")
        self.bot.edit_message_text.reset_mock()
        self.assertEqual((await edit_signal(self.bot, CHAT_ID, 0, signal(), "alert_expired")).reason,
                         "message_id_invalid")
        self.bot.edit_message_text.assert_not_awaited()

    async def test_reply_buttons_follow_persisted_state_through_expiry(self):
        alert = {**signal(), "reply_required": True}
        self.assertEqual((await send_signal(self.bot, CHAT_ID, alert)).status, "sent")
        buttons = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([button.text for button in buttons], ["Вошёл", "Пропуск"])
        enter_data = buttons[0].callback_data
        self.assertEqual(parse_reply_callback(enter_data), ("signal-01", "enter"))
        self.assertIsNone(parse_reply_callback("lq:b:signal-01"))

        for state, labels in (("waiting", ["Вошёл", "Пропуск"]), ("entered", ["Закрыл"]), ("closed", [])):
            await edit_signal(self.bot, CHAT_ID, 35, alert, "alert_expired", response_state=state)
            kwargs = self.bot.edit_message_text.await_args.kwargs
            self.assertIn("ИСТЁК", kwargs["text"])
            markup = kwargs["reply_markup"]
            actual = [button.text for row in markup.inline_keyboard for button in row] if markup else []
            self.assertEqual(actual, labels)

    async def test_non_usdt_price_uses_native_quote_when_plan_matches_pair(self):
        alert = {**signal(), "symbol": "ETH/BTC"}
        self.assertEqual((await send_signal(self.bot, CHAT_ID, alert)).reason, "signal_invalid")
        alert["plan"]["quote_asset"] = "BTC"
        self.assertEqual((await send_signal(self.bot, CHAT_ID, alert)).status, "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("12550.5 BTC", text)
        self.assertIn("Объём: 0.0008 ETH", text)
        self.assertIn("Риск по плану: 0.11 USDT", text)

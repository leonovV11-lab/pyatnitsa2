import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from telegram import Bot, Update
from telegram.constants import ChatType
from telegram.error import Conflict, RetryAfter, TimedOut

from live_reply_worker import check_reply_transport, run_replies

CHAT_ID = 1234567
MESSAGE_ID = 35
ROUTE = "telegram-route"


def chat(owner=CHAT_ID, chat_type=ChatType.PRIVATE):
    return SimpleNamespace(id=owner, type=chat_type)


def callback(update_id, code, *, user=CHAT_ID, target=None, message_id=MESSAGE_ID):
    message = SimpleNamespace(chat=target or chat(), message_id=message_id)
    query = SimpleNamespace(id=f"q{update_id}", data=f"lq:{code}:signal-01", from_user=chat(user), message=message)
    return SimpleNamespace(update_id=update_id, callback_query=query, message=None)


def command(update_id, *, user=CHAT_ID, target=None):
    message = SimpleNamespace(text="/skip_pending", chat=target or chat(), from_user=chat(user))
    return SimpleNamespace(update_id=update_id, callback_query=None, message=message)


class LiveReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = AsyncMock(spec=Bot)
        self.slots = Mock()
        self.wake = asyncio.Event()

    async def run_until_conflict(self, *updates):
        self.bot.get_updates.side_effect = [updates, Conflict("sensitive transport detail")]
        with patch("live_reply_worker.emit") as log, self.assertRaisesRegex(RuntimeError, "^telegram_polling_conflict$"):
            await run_replies(self.bot, CHAT_ID, ROUTE, self.slots, self.wake)
        self.assertNotIn("sensitive transport detail", str(log.call_args_list))
        self.bot.delete_webhook.assert_not_awaited()

    async def test_only_private_owner_may_use_buttons_on_matching_message(self):
        self.slots.respond.side_effect = ["invalid", "entered", "closed"]
        self.slots.state.side_effect = ["entered", "closed"]
        await self.run_until_conflict(
            callback(1, "e", user=CHAT_ID + 1),
            callback(2, "e", target=chat(chat_type=ChatType.GROUP)),
            callback(3, "e", target=chat(CHAT_ID + 1)),
            callback(4, "x"),
            callback(5, "e", message_id=MESSAGE_ID + 1),
            callback(6, "e"),
            callback(7, "c"),
        )
        self.assertEqual(self.slots.respond.call_args_list, [
            call("signal-01", "enter", route=ROUTE, message_id=MESSAGE_ID + 1),
            call("signal-01", "enter", route=ROUTE, message_id=MESSAGE_ID),
            call("signal-01", "close", route=ROUTE, message_id=MESSAGE_ID),
        ])
        self.assertEqual(self.bot.answer_callback_query.await_count, 3)
        first, second = self.bot.edit_message_reply_markup.await_args_list
        self.assertEqual(first.kwargs["reply_markup"].inline_keyboard[0][0].text, "Закрыл")
        self.assertIsNone(second.kwargs["reply_markup"])
        self.assertTrue(self.wake.is_set())
        self.assertEqual(self.bot.get_updates.await_args.kwargs["offset"], 8)
        self.assertEqual(self.bot.get_updates.await_args.kwargs["allowed_updates"],
                         (Update.CALLBACK_QUERY, Update.MESSAGE))

    async def test_skip_removes_buttons_and_wakes_scheduler(self):
        self.slots.respond.return_value = "skipped"
        self.slots.state.return_value = "skipped"
        await self.run_until_conflict(callback(1, "s"))
        self.slots.respond.assert_called_once_with("signal-01", "skip", route=ROUTE, message_id=MESSAGE_ID)
        self.assertIsNone(self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"])
        self.assertTrue(self.wake.is_set())

    async def test_keyboard_timeout_retries_without_repeating_ledger_change(self):
        self.slots.respond.return_value = "entered"
        self.slots.state.return_value = "entered"
        self.bot.edit_message_reply_markup.side_effect = [TimedOut("secret"), True]
        with patch("live_reply_worker.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.run_until_conflict(callback(1, "e"))
        self.slots.respond.assert_called_once()
        self.assertEqual(self.bot.edit_message_reply_markup.await_count, 2)
        sleep.assert_awaited_once_with(0.25)
        self.assertTrue(self.wake.is_set())

    async def test_skip_pending_command_is_private_and_owner_only(self):
        self.slots.skip_pending.return_value = "skipped"
        await self.run_until_conflict(command(1, user=CHAT_ID + 1), command(2), command(3, target=chat(123)))
        self.slots.skip_pending.assert_called_once_with(ROUTE)
        self.assertTrue(self.wake.is_set())
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], CHAT_ID)

    async def test_preflight_checks_existing_webhook_without_deleting_it(self):
        self.bot.get_webhook_info.return_value = SimpleNamespace(url="")
        self.assertEqual(await check_reply_transport(self.bot), "ready")
        self.bot.get_webhook_info.return_value = SimpleNamespace(url="https://example.com/private-token")
        self.assertEqual(await check_reply_transport(self.bot), "webhook_configured")
        self.bot.get_webhook_info.side_effect = TimedOut("secret")
        self.assertEqual(await check_reply_transport(self.bot), "telegram_unavailable")
        self.bot.delete_webhook.assert_not_awaited()
        self.bot.get_updates.assert_not_awaited()

    async def test_polling_retries_failures_without_leaking_details(self):
        self.bot.get_updates.side_effect = [TimedOut("secret"), RetryAfter(8), Conflict("secret")]
        with (
            patch("live_reply_worker.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("live_reply_worker.emit") as log,
            self.assertRaisesRegex(RuntimeError, "^telegram_polling_conflict$"),
        ):
            await run_replies(self.bot, CHAT_ID, ROUTE, self.slots, self.wake)
        self.assertEqual(sleep.await_args_list, [call(1), call(8)])
        self.assertNotIn("secret", str(log.call_args_list))
        self.assertFalse(self.wake.is_set())

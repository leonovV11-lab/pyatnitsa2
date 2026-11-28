import json
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from live_delivery import TelegramPublisher
from live_telegram import DeliveryResult
from news_live import LiveJournal, run
from official_news import NewsItem


def issue(journal, now, *, route="owner"):
    event = journal.observe(NewsItem(
        id="release", source="fed", publisher="Federal Reserve", title="FOMC statement",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=now - 3600, fetched_at=now - 1800, revision="revision",
    ))
    return journal.issue(event, "okx", "BTC/USDT", {"loss": 2.0}, cap=10, as_of=now, telegram_route=route)


def queue_setup(journal, now, *, expires_at):
    payload = {"type": "technical_setup", "id": "setup-alert"}
    with closing(journal.connect()) as db, db:
        db.execute("""
            INSERT INTO live_setups (id,event_id,revision,source,venue,symbol,issued_at,expires_at,status,payload)
            VALUES (?, 'release', 'revision', 'fed', 'okx', 'BTC/USDT', ?, ?, 'active', ?)
        """, (payload["id"], now, expires_at, json.dumps(payload)))
        journal.telegram.enqueue(db, payload["id"], "owner", now)
    return payload


class DeliveryJournalTests(unittest.TestCase):
    def test_technical_setup_uses_same_durable_outbox_without_reserving_trade(self):
        now = time.time()
        with TemporaryDirectory() as root:
            path = Path(root) / "news.sqlite3"
            writer = LiveJournal(path)
            setup = queue_setup(writer, now, expires_at=now + 5)
            reopened = LiveJournal(path)
            self.assertIsNone(reopened.telegram.claim_send("different-owner", now))
            self.assertEqual(writer.telegram.claim_send("owner", now), setup)
            self.assertIsNone(reopened.telegram.claim_send("owner", now))
            writer.telegram.finish_send(setup["id"], DeliveryResult("sent", message_id=21), now + 1)

            trade = issue(reopened, now + 1)
            self.assertIsNotNone(trade)
            self.assertEqual(reopened.telegram.claim_send("owner", now + 1), trade)
            self.assertEqual(reopened.telegram.claim_update("owner", now + 5), (setup, 21, "alert_expired"))
            self.assertIsNone(writer.telegram.claim_update("owner", now + 5))

    def test_expired_technical_setup_is_skipped_before_first_delivery(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "news.sqlite3")
            queue_setup(journal, now, expires_at=now + 5)
            self.assertIsNone(journal.telegram.claim_send("owner", now + 5))
            self.assertEqual(journal.telegram.counts(), {"skipped": 1})

    def test_only_one_process_can_claim_an_alert_for_its_recipient(self):
        now = time.time()
        with TemporaryDirectory() as root:
            path = Path(root) / "news.sqlite3"
            first = LiveJournal(path)
            alert = issue(first, now)
            second = LiveJournal(path)
            self.assertIsNone(second.telegram.claim_send("different-owner", now))
            self.assertIsNotNone(first.telegram.claim_send("owner", now))
            self.assertIsNone(second.telegram.claim_send("owner", now))
            first.telegram.finish_send(alert["id"], DeliveryResult("sent", message_id=21), now + 1)
            self.assertEqual(second.telegram.counts()["sent"], 1)
            self.assertIsNone(second.telegram.claim_send("owner", now + 2))

    def test_lost_confirmation_is_never_resent_after_restart(self):
        now = time.time()
        with TemporaryDirectory() as root:
            path = Path(root) / "news.sqlite3"
            first = LiveJournal(path)
            issue(first, now)
            self.assertIsNotNone(first.telegram.claim_send("owner", now))
            second = LiveJournal(path)
            self.assertIsNone(second.telegram.claim_send("owner", now + 30))
            self.assertEqual(second.telegram.counts()["unconfirmed"], 1)

    def test_expired_signal_is_skipped(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "news.sqlite3")
            issue(journal, now)
            self.assertIsNone(journal.telegram.claim_send("owner", now + 6))
            self.assertEqual(journal.telegram.counts()["skipped"], 1)

    def test_only_explicitly_rate_limited_response_can_retry_before_expiration(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "news.sqlite3")
            alert = issue(journal, now)
            self.assertIsNotNone(journal.telegram.claim_send("owner", now))
            journal.telegram.finish_send(alert["id"], DeliveryResult("rate_limited", retry_after=1), now + 1)
            self.assertIsNone(journal.telegram.claim_send("owner", now + 1.5))
            self.assertIsNotNone(journal.telegram.claim_send("owner", now + 2.5))
            journal.telegram.finish_send(alert["id"], DeliveryResult("rate_limited", retry_after=1), now + 2.5)
            self.assertIsNone(journal.telegram.claim_send("owner", now + 3.5))


class TelegramPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_check_verifies_private_chat_without_running_monitor_or_sending(self):
        options = SimpleNamespace(telegram=False, telegram_check=True, telegram_chat_id=1234)
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        with (
            patch("news_live.Bot", return_value=bot),
            patch("news_live.verify_chat", new=AsyncMock(return_value="ready")) as check,
            patch("news_live.run_monitor", new=AsyncMock()) as monitor,
            patch("news_live.emit") as output,
        ):
            self.assertTrue(await run(options, None))
        check.assert_awaited_once_with(bot, 1234)
        monitor.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        output.assert_called_once_with({"type": "telegram_check", "status": "ready"})

    async def test_unverified_destination_stops_worker(self):
        options = SimpleNamespace(telegram=True, telegram_check=False, telegram_chat_id=1234)
        bot = AsyncMock()
        with (
            patch("news_live.Bot", return_value=bot),
            patch("news_live.verify_chat", new=AsyncMock(return_value="chat_not_private")),
            patch("news_live.run_monitor", new=AsyncMock()) as monitor,
            patch("news_live.emit"),
        ):
            self.assertFalse(await run(options, None))
        monitor.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_publisher_sends_to_owner_then_edits_the_same_message_once(self):
        now = time.time()
        with TemporaryDirectory() as root:
            journal = LiveJournal(Path(root) / "news.sqlite3")
            alert = issue(journal, now)
            publisher = TelegramPublisher(object(), 1234, "owner", journal.telegram)
            with (
                patch("live_delivery.send_signal", new=AsyncMock(return_value=DeliveryResult("sent", message_id=42)))
                as send,
                patch("live_delivery.edit_signal", new=AsyncMock(return_value=DeliveryResult("sent"))) as edit,
                patch("live_delivery.emit"),
            ):
                await publisher.flush()
                self.assertEqual(send.await_args.args[1:3], (1234, alert))
                edit.assert_not_awaited()
                journal.invalidate({"fed": "timeout"}, now + 1)
                await publisher.flush()
                await publisher.flush()
                edit.assert_awaited_once()
                self.assertEqual(edit.await_args.args[1:3], (1234, 42))
                self.assertEqual(edit.await_args.args[-1], "source_unavailable")
                self.assertEqual(send.await_count, 1)

import asyncio
import json
import unittest
from typing import TypedDict
from unittest.mock import patch

from news_agent_team import analyze_team


class Body:
    def __init__(self, data):
        self.data = data

    async def readexactly(self, size):
        if len(self.data) < size:
            raise asyncio.IncompleteReadError(self.data, size)
        return self.data[:size]


class Response:
    def __init__(self, session, behavior):
        self.session = session
        self.status = 200
        self.behavior = behavior
        answer = {"decision": "watch", "reason": "Мало событий", "pnl": "Два бумажных исхода"}
        content = json.dumps(answer) if behavior != "malformed" else "{{"
        data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        if behavior == "oversized":
            data = b" " * 100_000
        if behavior == "rate_limited":
            self.status = 429
        self.content = Body(data)

    async def __aenter__(self):
        self.session.active += 1
        self.session.peak = max(self.session.peak, self.session.active)
        try:
            await asyncio.sleep(0.05 if self.behavior == "timeout" else 0)
        except BaseException:
            self.session.active -= 1
            raise
        return self

    async def __aexit__(self, *_args):
        self.session.active -= 1


class Session:
    def __init__(self, behaviors=None):
        self.calls = []
        self.active = 0
        self.peak = 0
        self.behaviors = behaviors or {}

    def post(self, url, **kwargs):
        index = len(self.calls)
        self.calls.append((url, kwargs))
        return Response(self, self.behaviors.get(index))


class ProviderConfig(TypedDict):
    url: str
    key: str
    model: str


PARAMETERS: ProviderConfig = {
    "url": "https://example.test/v1/chat/completions", "key": "test-only", "model": "fixture",
}
EVIDENCE = {"event": {"status": "confirmed"}, "market": {"bid": 99}, "paper_results": [{"pnl": -1.5}]}


class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifty_agents_have_distinct_prompts_and_share_paper_evidence(self):
        session = Session()
        rows = await analyze_team(session, EVIDENCE, concurrency=50, **PARAMETERS)
        self.assertEqual(session.peak, 50)
        self.assertEqual(len(rows), 50)
        self.assertEqual(len({row["agent"] for row in rows}), 50)
        self.assertEqual(len({row["group"] for row in rows}), 5)
        self.assertEqual(len(session.calls), 50)
        prompts = [call[1]["json"]["messages"][0]["content"] for call in session.calls]
        self.assertEqual(len(set(prompts)), 50)
        for _, request in session.calls:
            self.assertEqual(json.loads(request["json"]["messages"][1]["content"]), EVIDENCE)
            self.assertFalse(request["allow_redirects"])
            self.assertNotIn("tools", request["json"])
        self.assertEqual({row["decision"] for row in rows}, {"watch"})

    async def test_overlapping_batches_share_a_concurrency_limit(self):
        session = Session()
        batches = await asyncio.gather(
            analyze_team(session, EVIDENCE, concurrency=4, **PARAMETERS),
            analyze_team(session, EVIDENCE, concurrency=4, **PARAMETERS),
        )
        self.assertEqual([len(rows) for rows in batches], [50, 50])
        self.assertEqual(len(session.calls), 100)
        self.assertEqual(session.peak, 4)

    async def test_live_context_uses_recorded_trade_statistics(self):
        session = Session()
        evidence = {"mode": "live", "recorded_results": {"provenance": "recorded_fill", "event_count": 22}}
        rows = await analyze_team(session, evidence, concurrency=50, **PARAMETERS)
        self.assertEqual(len(rows), 50)
        self.assertEqual(session.peak, 50)
        for _, request in session.calls:
            messages = request["json"]["messages"]
            self.assertIn("operator-supplied real-fill records", messages[0]["content"])
            self.assertNotIn("simulation", messages[0]["content"])
            self.assertEqual(json.loads(messages[1]["content"]), evidence)

    async def test_malformed_responses_rate_limits_and_timeouts_are_unavailable(self):
        session = Session({0: "malformed", 2: "oversized", 4: "rate_limited", 6: "timeout"})
        with patch("news_agent_team.MAX_ROUND_TRIP_SECONDS", 0.02):
            rows = await analyze_team(session, EVIDENCE, **PARAMETERS)
        self.assertEqual(len(rows), 50)
        self.assertEqual(sum(row["decision"] == "unavailable" for row in rows), 4)
        self.assertEqual(len(session.calls), 50)
        self.assertEqual(session.active, 0)

    async def test_bad_provider_addresses_never_receive_credentials(self):
        for url in ("", "http://host", "https:/not-a-host", "https://a:b@host.test"):
            with self.subTest(url=url):
                session = Session()
                config: ProviderConfig = {**PARAMETERS, "url": url}
                rows = await analyze_team(session, EVIDENCE, **config)
                self.assertEqual(len(rows), 50)
                self.assertEqual({row["decision"] for row in rows}, {"unavailable"})
                self.assertEqual(session.calls, [])

    async def test_cancellation_releases_all_slots_and_allows_a_new_limit(self):
        session = Session(dict.fromkeys(range(50), "timeout"))
        batch = asyncio.create_task(analyze_team(session, EVIDENCE, concurrency=4, **PARAMETERS))
        async with asyncio.timeout(1):
            while session.active < 4:
                await asyncio.sleep(0)
        batch.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await batch
        self.assertEqual(session.active, 0)
        fresh = Session()
        await analyze_team(fresh, EVIDENCE, concurrency=7, **PARAMETERS)
        self.assertEqual(fresh.peak, 7)

    async def test_missing_excessive_or_nonfinite_evidence_never_reaches_provider(self):
        bad = ({}, {"bad": float("nan")}, {"too_much": "x" * 50_000})
        for evidence in bad:
            with self.subTest(kind=next(iter(evidence), None)):
                session = Session()
                rows = await analyze_team(session, evidence, **PARAMETERS)
                self.assertEqual(len(rows), 50)
                self.assertTrue(all(row["decision"] == "unavailable" for row in rows))
                self.assertEqual(session.calls, [])

    async def test_invalid_concurrency_cannot_schedule_requests(self):
        for concurrency in (0, 51, True):
            session = Session()
            rows = await analyze_team(session, EVIDENCE, concurrency=concurrency, **PARAMETERS)
            self.assertEqual({row["decision"] for row in rows}, {"unavailable"})
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()

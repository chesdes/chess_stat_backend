import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models import Game, Player
from services import AnalysisService, GameNotFoundError


def make_game(url="https://www.chess.com/game/live/123456"):
    return Game(
        site="chesscom",
        white=Player(username="alice", rating=1500, result="win"),
        black=Player(username="bob", rating=1400, result="loss"),
        end_time=datetime.now(timezone.utc),
        pgn='[White "alice"]\n[Black "bob"]\n\n1. e4 e5',
        url=url,
    )


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        value = self.store.get(key)
        if value is None:
            return None
        return value.encode("utf-8") if isinstance(value, str) else value

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value
        return True


class StubAnalyzer:
    def __init__(self, result):
        self._result = result

    def calculate_info(self, results, pgn):
        return self._result


def run(coro):
    return asyncio.run(coro)


class SharedSnapshotTests(unittest.TestCase):
    def test_save_mints_fresh_id_each_time(self):
        game = make_game()
        result = [{"move": "e2e4"}]
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game]),
        })()
        redis = FakeRedis()

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                first = await service.save_last_game_analysis(
                    "chesscom", "alice", 1, SimpleNamespace(results=[]),
                    request=SimpleNamespace(state=SimpleNamespace()),
                )
                second = await service.save_last_game_analysis(
                    "chesscom", "alice", 1, SimpleNamespace(results=[]),
                    request=SimpleNamespace(state=SimpleNamespace()),
                )
                return first, second

        # StubAnalyzer ignores payload; bypass pydantic payload via model_dump patch
        with patch.object(StubAnalyzer, "calculate_info", return_value=result):
            first, second = run(flow())

        self.assertIsNotNone(first["share_id"])
        self.assertIsNotNone(second["share_id"])
        self.assertNotEqual(first["share_id"], second["share_id"])
        # Old link still resolves to the first snapshot (no overwrite of snapshots).
        async def fetch_both():
            with patch("utils.RedisClient.get_client", return_value=redis):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                a = await service.get_shared_analysis(first["share_id"])
                b = await service.get_shared_analysis(second["share_id"])
                return a, b

        a, b = run(fetch_both())
        self.assertEqual(a["analyze"], result)
        self.assertEqual(b["analyze"], result)

    def test_get_last_backfills_share_id_then_reuses_latest(self):
        game = make_game()
        cached = [{"move": "e2e4"}]
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game]),
        })()
        redis = FakeRedis()
        redis.store["analyze:chesscom:123456"] = json.dumps(cached)

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(cached))
                first = await service.get_last_game_analysis("chesscom", "alice", 1)
                second = await service.get_last_game_analysis("chesscom", "alice", 1)
                return first, second

        first, second = run(flow())
        self.assertIsNotNone(first["share_id"])
        self.assertEqual(first["share_id"], second["share_id"])
        self.assertEqual(first["analyze"], cached)

    def test_get_shared_rejects_bad_id_and_missing_snapshot(self):
        redis = FakeRedis()

        async def flow():
            with patch("utils.RedisClient.get_client", return_value=redis):
                service = AnalysisService(analyzer=StubAnalyzer([]))
                with self.assertRaises(ValueError):
                    await service.get_shared_analysis("??bad??")
                with self.assertRaises(GameNotFoundError):
                    await service.get_shared_analysis("Abc123XyZ")

        run(flow())

    def test_analyze_pgn_builds_game_and_share(self):
        pgn = (
            '[White "alice"]\n[Black "bob"]\n[WhiteElo "1600"]\n[BlackElo "1500"]\n'
            '[Result "1-0"]\n[UTCDate "2026.09.01"]\n[UTCTime "10:00:00"]\n'
            '[TimeControl "600"]\n\n1. e4 e5 1-0'
        )
        result = [{"move": "e2e4"}, {"move": "e7e5"}]
        redis = FakeRedis()

        async def flow():
            with patch("utils.RedisClient.get_client", return_value=redis):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                payload = SimpleNamespace(
                    pgn=pgn,
                    results=[SimpleNamespace(model_dump=lambda: {"info": {"type": "cp", "value": 10}, "best_move": "e2e4"})] * 2,
                )
                saved = await service.analyze_pgn(payload)
                fetched = await service.get_shared_analysis(saved["share_id"])
                return saved, fetched

        saved, fetched = run(flow())
        self.assertEqual(saved["game"].white.username, "alice")
        self.assertEqual(saved["game"].white.result, "win")
        self.assertEqual(saved["game"].site, "pgn")
        self.assertIsNone(saved["game"].url)
        self.assertEqual(fetched["game"].white.username, "alice")
        self.assertEqual(fetched["analyze"], result)


if __name__ == "__main__":
    unittest.main()

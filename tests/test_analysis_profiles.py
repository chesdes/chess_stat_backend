import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models import Game, Player, PlayerCard
from parsers.base_parser import PlayerNotFoundError
from services import AnalysisService


def make_game(url="https://www.chess.com/game/live/123456", white="alice", black="bob"):
    return Game(
        site="chesscom",
        white=Player(username=white, rating=1500, result="win"),
        black=Player(username=black, rating=1400, result="loss"),
        end_time=datetime.now(timezone.utc),
        pgn='[White "alice"]\n[Black "bob"]\n\n1. e4 e5',
        url=url,
    )


def make_card(username, title="GM"):
    return PlayerCard(
        site="chesscom",
        username=username,
        avatar=f"https://avatars/{username}.png",
        country="US",
        title=title,
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


def parser_with(cards=None, fail=None):
    """Parser stub: get_last_games returns [game], get_profile_card per map/fail."""
    cards = cards or {}
    fail = fail or set()

    async def get_profile_card(username):
        if username in fail:
            raise PlayerNotFoundError(username)
        if username in cards:
            return cards[username]
        return make_card(username, title=None)

    return type("Parser", (), {
        "get_last_games": AsyncMock(return_value=[make_game()]),
        "get_profile_card": AsyncMock(side_effect=get_profile_card),
    })()


class AnalysisProfilesTests(unittest.TestCase):
    def test_save_embeds_profiles_and_shared_roundtrip(self):
        game = make_game()
        result = [{"move": "e2e4"}]
        parser = parser_with(cards={"alice": make_card("alice"), "bob": make_card("bob", title=None)})
        redis = FakeRedis()

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                saved = await service.save_last_game_analysis(
                    "chesscom", "alice", 1, SimpleNamespace(results=[]),
                    request=SimpleNamespace(state=SimpleNamespace()),
                )
                fetched = await service.get_shared_analysis(saved["share_id"])
                return saved, fetched

        with patch.object(StubAnalyzer, "calculate_info", return_value=result):
            saved, fetched = run(flow())

        self.assertEqual(saved["profiles"]["white"].username, "alice")
        self.assertEqual(saved["profiles"]["white"].title, "GM")
        self.assertEqual(saved["profiles"]["black"].username, "bob")
        # Snapshot round-trip keeps the cards.
        self.assertEqual(fetched["profiles"]["white"].username, "alice")
        self.assertEqual(fetched["profiles"]["black"].avatar, "https://avatars/bob.png")

    def test_profile_failure_is_best_effort(self):
        result = [{"move": "e2e4"}]
        parser = parser_with(cards={"alice": make_card("alice")}, fail={"bob"})
        redis = FakeRedis()

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                return await service.save_last_game_analysis(
                    "chesscom", "alice", 1, SimpleNamespace(results=[]),
                    request=SimpleNamespace(state=SimpleNamespace()),
                )

        with patch.object(StubAnalyzer, "calculate_info", return_value=result):
            saved = run(flow())

        self.assertEqual(saved["analyze"], result)
        self.assertIsNotNone(saved["share_id"])
        self.assertEqual(saved["profiles"]["white"].username, "alice")
        self.assertIsNone(saved["profiles"]["black"])

    def test_get_last_returns_profiles_on_hit_and_miss(self):
        game = make_game()
        cached = [{"move": "e2e4"}]
        parser = parser_with(cards={"alice": make_card("alice"), "bob": make_card("bob", title=None)})
        redis = FakeRedis()
        redis.store["analyze:chesscom:123456"] = json.dumps(cached)

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(cached))
                hit = await service.get_last_game_analysis("chesscom", "alice", 1)
                return hit

        hit = run(flow())
        self.assertEqual(hit["analyze"], cached)
        self.assertEqual(hit["profiles"]["white"].username, "alice")
        self.assertEqual(hit["profiles"]["black"].username, "bob")

        # Miss path (empty cache) also carries profiles with analyze=None.
        redis2 = FakeRedis()
        parser2 = parser_with(cards={"alice": make_card("alice"), "bob": make_card("bob", title=None)})

        async def flow2():
            with (
                patch("services.analysis_service.get_parser", return_value=parser2),
                patch("utils.RedisClient.get_client", return_value=redis2),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(cached))
                return await service.get_last_game_analysis("chesscom", "alice", 1)

        miss = run(flow2())
        self.assertIsNone(miss["analyze"])
        self.assertIsNone(miss["share_id"])
        self.assertEqual(miss["profiles"]["white"].username, "alice")

    def test_pgn_has_null_profiles(self):
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
                return await service.analyze_pgn(payload)

        saved = run(flow())
        self.assertEqual(saved["profiles"], {"white": None, "black": None})

    def test_old_snapshot_without_profiles_reads_as_nones(self):
        game = make_game()
        result = [{"move": "e2e4"}]
        redis = FakeRedis()
        # Legacy payload: no "profiles" key at all.
        legacy = json.dumps({"game": game.model_dump(mode="json"), "analyze": result, "site": "chesscom"})
        redis.store["analyze:shared:Legacy123"] = legacy

        async def flow():
            with patch("utils.RedisClient.get_client", return_value=redis):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                return await service.get_shared_analysis("Legacy123")

        fetched = run(flow())
        self.assertEqual(fetched["analyze"], result)
        self.assertEqual(fetched["profiles"], {"white": None, "black": None})

    def test_same_player_fetches_once(self):
        game = make_game(white="alice", black="Alice")
        result = [{"move": "e2e4"}]
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game]),
            "get_profile_card": AsyncMock(side_effect=lambda username: make_card("alice")),
        })()
        redis = FakeRedis()

        async def flow():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                service = AnalysisService(analyzer=StubAnalyzer(result))
                with patch.object(StubAnalyzer, "calculate_info", return_value=result):
                    return await service.save_last_game_analysis(
                        "chesscom", "alice", 1, SimpleNamespace(results=[]),
                        request=SimpleNamespace(state=SimpleNamespace()),
                    )

        saved = run(flow())
        self.assertEqual(parser.get_profile_card.await_count, 1)
        self.assertEqual(saved["profiles"]["white"].username, "alice")
        self.assertEqual(saved["profiles"]["black"].username, "alice")


if __name__ == "__main__":
    unittest.main()

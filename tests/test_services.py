import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models import Game, Player
from services import AnalysisService, GamesService


class GamesServiceTests(unittest.TestCase):
    def test_page_returns_limited_games_and_has_more(self):
        game_one = self.make_game("game-1")
        game_two = self.make_game("game-2")
        extra_game = self.make_game("extra")
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game_one, game_two, extra_game]),
        })()

        async def run():
            with patch("services.games_service.get_parser", return_value=parser):
                return await GamesService().get_games_page("chesscom", "player", 2, 4)

        page = asyncio.run(run())

        self.assertEqual(page.games, [game_one, game_two])
        self.assertEqual(page.offset, 4)
        self.assertEqual(page.limit, 2)
        # More pages ahead: the true total is unknown, so no page count.
        self.assertIsNone(page.total_pages)
        self.assertTrue(page.has_more)
        parser.get_last_games.assert_awaited_once_with(
            username="player",
            limit=3,
            offset=4,
        )

    def test_last_page_reports_exact_total_pages(self):
        game_one = self.make_game("game-1")
        game_two = self.make_game("game-2")
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game_one, game_two]),
        })()

        async def run():
            with patch("services.games_service.get_parser", return_value=parser):
                return await GamesService().get_games_page("chesscom", "player", 10, 20)

        page = asyncio.run(run())

        # 22 games at limit 10 -> exactly 3 pages, no more ahead.
        self.assertEqual(page.total_pages, 3)
        self.assertFalse(page.has_more)

    @staticmethod
    def make_game(username):
        return Game(
            site="chesscom",
            white=Player(username=username, rating=1500, result="win"),
            black=Player(username="opponent", rating=1500, result="loss"),
            end_time=datetime.now(timezone.utc),
            pgn="1. e4 e5",
        )


class AnalysisServiceCacheTests(unittest.TestCase):
    def test_cache_hit_records_moves_count(self):
        game = GamesServiceTests.make_game("player")
        game.url = "https://www.chess.com/game/live/123"
        cached = [{"move": "e2e4"}, {"move": "e7e5"}, {"move": "g1f3"}]
        parser = type("Parser", (), {
            "get_last_games": AsyncMock(return_value=[game]),
        })()
        redis = type("Redis", (), {
            "get": AsyncMock(return_value=json.dumps(cached)),
        })()
        request = SimpleNamespace(state=SimpleNamespace())

        async def run():
            with (
                patch("services.analysis_service.get_parser", return_value=parser),
                patch("utils.RedisClient.get_client", return_value=redis),
            ):
                return await AnalysisService().get_last_game_analysis(
                    "chesscom", "player", 1, request=request
                )

        result = asyncio.run(run())

        self.assertEqual(result["analyze"], cached)
        self.assertTrue(request.state.analysis_cache_hit)
        self.assertEqual(request.state.analysis_moves_count, len(cached))


if __name__ == "__main__":
    unittest.main()

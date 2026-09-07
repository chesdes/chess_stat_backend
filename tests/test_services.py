import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from models import Game, Player
from services import GamesService


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
        self.assertEqual(page.total_pages, 150)
        self.assertTrue(page.has_more)
        parser.get_last_games.assert_awaited_once_with(
            username="player",
            limit=3,
            offset=4,
        )

    @staticmethod
    def make_game(username):
        return Game(
            site="chesscom",
            white=Player(username=username, rating=1500, result="win"),
            black=Player(username="opponent", rating=1500, result="loss"),
            end_time=datetime.now(timezone.utc),
            pgn="1. e4 e5",
        )


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest
from datetime import datetime, timezone

from features.performance import get_performance
from models import Game, Player


class PerformanceTests(unittest.TestCase):
    def make_game(self, white_result="draw", black_result="draw"):
        return Game(
            site="chesscom",
            white=Player(username="Player", rating=1500, result=white_result),
            black=Player(username="Opponent", rating=1500, result=black_result),
            end_time=datetime.now(timezone.utc),
            pgn="",
        )

    def test_empty_games_return_zero(self):
        result = asyncio.run(get_performance([], "player"))

        self.assertEqual(result, 0)

    def test_even_score_against_equal_opponent_returns_equal_rating(self):
        result = asyncio.run(get_performance([self.make_game()], "PLAYER"))

        self.assertEqual(result, 1500)

    def test_win_is_counted_for_white_player(self):
        result = asyncio.run(get_performance([self.make_game("win", "loss")], "player"))

        self.assertGreater(result, 1500)


if __name__ == "__main__":
    unittest.main()

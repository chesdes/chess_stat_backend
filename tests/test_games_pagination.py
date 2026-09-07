import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from parsers.chesscom import ChessComParser


class GamesPaginationTests(unittest.TestCase):
    def test_get_last_games_applies_offset_before_limit(self):
        games = [
            self.make_game("oldest", 1),
            self.make_game("middle", 2),
            self.make_game("newest", 3),
        ]
        responses = [
            {"archives": ["https://example.test/archive"]},
            {"games": games},
        ]

        async def run():
            with patch("parsers.chesscom.get_json", new=AsyncMock(side_effect=responses)):
                return await ChessComParser().get_last_games(
                    username="player",
                    limit=1,
                    offset=1,
                )

        result = asyncio.run(run())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].white.username, "middle")

    def test_period_query_does_not_skip_february_from_january_31(self):
        start = int(datetime(2024, 1, 31, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp())

        async def run(mock_get_json):
            with self.assertRaises(ValueError):
                await ChessComParser().get_games_in_period("player", start, end)
            return [call.args[0] for call in mock_get_json.call_args_list]

        mock_get_json = AsyncMock(return_value={"games": []})

        async def collect_urls():
            with patch("parsers.chesscom.get_json", new=mock_get_json):
                return await run(mock_get_json)

        urls = asyncio.run(collect_urls())

        self.assertEqual(
            urls,
            [
                "https://api.chess.com/pub/player/player/games/2024/01",
                "https://api.chess.com/pub/player/player/games/2024/02",
                "https://api.chess.com/pub/player/player/games/2024/03",
            ],
        )

    @staticmethod
    def make_game(username, end_time):
        return {
            "white": {"username": username, "rating": 1500, "result": "win"},
            "black": {"username": "opponent", "rating": 1500, "result": "loss"},
            "end_time": end_time,
            "time_class": "rapid",
            "pgn": "1. e4 e5",
            "url": f"https://example.test/{username}",
        }


if __name__ == "__main__":
    unittest.main()

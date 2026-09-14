import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from parsers import PlayerNotFoundError
from parsers.chesscom import ChessComParser
from utils import UpstreamHTTPError


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
            result = await ChessComParser().get_games_in_period("player", start, end)
            return result, [call.args[0] for call in mock_get_json.call_args_list]

        mock_get_json = AsyncMock(return_value={"games": []})

        async def collect_urls():
            with patch("parsers.chesscom.get_json", new=mock_get_json):
                return await run(mock_get_json)

        result, urls = asyncio.run(collect_urls())

        self.assertEqual(result, [])

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


class PlayerNotFoundTests(unittest.TestCase):
    ARCHIVES_URL = "https://api.chess.com/pub/player/ghost/games/archives"

    def test_archives_404_raises_player_not_found(self):
        async def run():
            with patch(
                "parsers.chesscom.get_json",
                new=AsyncMock(side_effect=UpstreamHTTPError(404, self.ARCHIVES_URL)),
            ):
                with self.assertRaises(PlayerNotFoundError):
                    await ChessComParser().get_last_games(username="ghost", limit=1)

        asyncio.run(run())

    def test_profile_404_raises_player_not_found(self):
        async def run():
            with patch(
                "parsers.chesscom.get_json",
                new=AsyncMock(
                    side_effect=UpstreamHTTPError(
                        404, "https://api.chess.com/pub/player/ghost"
                    )
                ),
            ):
                with self.assertRaises(PlayerNotFoundError):
                    await ChessComParser().get_profile(username="ghost")

        asyncio.run(run())

    def test_flaky_month_404_is_skipped(self):
        games = [GamesPaginationTests.make_game("newest", 3)]

        async def run():
            with patch(
                "parsers.chesscom.get_json",
                new=AsyncMock(
                    side_effect=[
                        {"archives": ["https://example.test/old", "https://example.test/new"]},
                        UpstreamHTTPError(404, "https://example.test/old"),
                        {"games": games},
                    ]
                ),
            ):
                return await ChessComParser().get_last_games(username="player", limit=5)

        result = asyncio.run(run())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].white.username, "newest")

    def test_non_404_upstream_error_still_raises(self):
        async def run():
            with patch(
                "parsers.chesscom.get_json",
                new=AsyncMock(side_effect=UpstreamHTTPError(502, self.ARCHIVES_URL)),
            ):
                with self.assertRaises(UpstreamHTTPError):
                    await ChessComParser().get_last_games(username="ghost", limit=1)

        asyncio.run(run())

    def test_period_all_months_404_with_missing_player(self):
        start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp())

        async def run():
            def route(url):
                if url.endswith("/archives"):
                    raise UpstreamHTTPError(404, url)
                raise UpstreamHTTPError(404, url)

            with patch("parsers.chesscom.get_json", new=AsyncMock(side_effect=route)):
                with self.assertRaises(PlayerNotFoundError):
                    await ChessComParser().get_games_in_period("ghost", start, end)

        asyncio.run(run())

    def test_period_all_months_404_with_existing_player_returns_empty(self):
        start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp())

        async def run():
            def route(url):
                if url.endswith("/archives"):
                    return {"archives": []}
                raise UpstreamHTTPError(404, url)

            with patch("parsers.chesscom.get_json", new=AsyncMock(side_effect=route)):
                return await ChessComParser().get_games_in_period("idle", start, end)

        self.assertEqual(asyncio.run(run()), [])


class UpstreamErrorUrlTests(unittest.TestCase):
    def test_url_is_attached_and_optional(self):
        err = UpstreamHTTPError(404, "https://example.test/x")
        self.assertEqual(err.url, "https://example.test/x")
        self.assertIn("https://example.test/x", str(err))
        self.assertIsNone(UpstreamHTTPError(404).url)


if __name__ == "__main__":
    unittest.main()

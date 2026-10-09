import asyncio
import gzip
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from config import (
    ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS,
    ARCHIVE_CACHE_TTL_SECONDS,
    ARCHIVE_CURRENT_MONTH_TTL_SECONDS,
    ARCHIVE_LIST_CACHE_TTL_SECONDS,
)
from parsers.chesscom import ChessComParser
from utils import UpstreamHTTPError
from utils import archive_cache

SITE = "chesscom"
MONTH_DATA = {
    "games": [
        {
            "white": {"username": "player", "rating": 1500, "result": "win"},
            "black": {"username": "opponent", "rating": 1500, "result": "loss"},
            "end_time": 1,
            "time_class": "rapid",
            "pgn": "1. e4 e5",
            "url": "https://www.chess.com/game/live/123456",
        }
    ]
}


def past_month():
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - 3
    return total // 12, total % 12 + 1


def current_month():
    now = datetime.now(timezone.utc)
    return now.year, now.month


def month_url(username, year, month):
    return f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"


class StubRedis:
    def __init__(self):
        self.store = {}
        self.set_calls = []
        self.getex_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def getex(self, key, ex=None):
        self.getex_calls.append((key, ex))
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self.store[key] = value


def patch_redis(stub=None):
    if stub is None:
        return patch(
            "utils.archive_cache.RedisClient.get_client",
            side_effect=RuntimeError("Redis not initialized"),
        )
    return patch("utils.archive_cache.RedisClient.get_client", return_value=stub)


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        archive_cache._inflight.clear()

    def test_second_read_served_from_cache(self):
        stub = StubRedis()
        year, month = past_month()
        url = month_url("player", year, month)

        async def run():
            with patch_redis(stub):
                first = await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )
                blocked_fetch = AsyncMock(side_effect=AssertionError("cache miss on second read"))
                second = await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: blocked_fetch(),
                )
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(first, MONTH_DATA)
        self.assertEqual(second, MONTH_DATA)
        key = f"archive:{SITE}:player:{year:04d}:{month:02d}"
        self.assertEqual(json.loads(gzip.decompress(stub.store[key])), MONTH_DATA)

    def test_past_month_read_refreshes_ttl(self):
        stub = StubRedis()
        year, month = past_month()
        url = month_url("player", year, month)

        async def run():
            with patch_redis(stub):
                await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )
                await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )

        asyncio.run(run())

        key = f"archive:{SITE}:player:{year:04d}:{month:02d}"
        self.assertEqual(stub.getex_calls, [(key, ARCHIVE_CACHE_TTL_SECONDS), (key, ARCHIVE_CACHE_TTL_SECONDS)])
        self.assertEqual(stub.set_calls[0][2], ARCHIVE_CACHE_TTL_SECONDS)

    def test_current_month_uses_fixed_ttl_and_no_refresh(self):
        stub = StubRedis()
        year, month = current_month()
        url = month_url("player", year, month)

        async def run():
            with patch_redis(stub):
                first = await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )
                second = await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(first, MONTH_DATA)
        self.assertEqual(second, MONTH_DATA)
        self.assertEqual(stub.getex_calls, [])
        key = f"archive:{SITE}:player:{year:04d}:{month:02d}"
        self.assertEqual(stub.set_calls, [(key, stub.store[key], ARCHIVE_CURRENT_MONTH_TTL_SECONDS)])

    def test_404_negative_cached_for_past_month(self):
        stub = StubRedis()
        year, month = past_month()
        url = month_url("ghost", year, month)
        error = UpstreamHTTPError(404, url)

        async def run():
            with patch_redis(stub):
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(
                        SITE, "ghost", year, month, url, lambda: AsyncMock(side_effect=error)(),
                    )
                blocked_fetch = AsyncMock(side_effect=AssertionError("fetch on negative cache hit"))
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(
                        SITE, "ghost", year, month, url, lambda: blocked_fetch(),
                    )

        asyncio.run(run())

        miss_key = f"archive:miss:{SITE}:ghost:{year:04d}:{month:02d}"
        self.assertEqual(stub.set_calls, [(miss_key, b"1", ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS)])

    def test_404_not_cached_for_current_month(self):
        stub = StubRedis()
        year, month = current_month()
        url = month_url("player", year, month)
        error = UpstreamHTTPError(404, url)
        fetch = AsyncMock(side_effect=error)

        async def run():
            with patch_redis(stub):
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(SITE, "player", year, month, url, lambda: fetch())
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(SITE, "player", year, month, url, lambda: fetch())

        asyncio.run(run())

        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(stub.set_calls, [])

    def test_non_404_error_not_cached(self):
        stub = StubRedis()
        year, month = past_month()
        url = month_url("player", year, month)
        error = UpstreamHTTPError(429, url)
        fetch = AsyncMock(side_effect=error)

        async def run():
            with patch_redis(stub):
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(SITE, "player", year, month, url, lambda: fetch())
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_month_archive(SITE, "player", year, month, url, lambda: fetch())

        asyncio.run(run())

        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(stub.set_calls, [])

    def test_concurrent_requests_share_single_fetch(self):
        stub = StubRedis()
        year, month = past_month()
        url = month_url("player", year, month)
        calls = []

        async def slow_fetch():
            calls.append(url)
            await asyncio.sleep(0.05)
            return MONTH_DATA

        async def run():
            with patch_redis(stub):
                return await asyncio.gather(
                    archive_cache.get_month_archive(SITE, "player", year, month, url, slow_fetch),
                    archive_cache.get_month_archive(SITE, "player", year, month, url, slow_fetch),
                )

        results = asyncio.run(run())

        self.assertEqual(results, [MONTH_DATA, MONTH_DATA])
        self.assertEqual(calls, [url])

    def test_redis_unavailable_falls_back_to_fetch(self):
        year, month = past_month()
        url = month_url("player", year, month)
        fetch = AsyncMock(return_value=MONTH_DATA)

        async def run():
            with patch_redis(None):
                return await archive_cache.get_month_archive(SITE, "player", year, month, url, lambda: fetch())

        result = asyncio.run(run())

        self.assertEqual(result, MONTH_DATA)
        self.assertEqual(fetch.await_count, 1)

    def test_corrupt_cache_value_is_refetched(self):
        stub = StubRedis()
        year, month = past_month()
        key = f"archive:{SITE}:player:{year:04d}:{month:02d}"
        stub.store[key] = b"not-gzip"
        url = month_url("player", year, month)

        async def run():
            with patch_redis(stub):
                return await archive_cache.get_month_archive(
                    SITE, "player", year, month, url, lambda: AsyncMock(return_value=MONTH_DATA)(),
                )

        result = asyncio.run(run())

        self.assertEqual(result, MONTH_DATA)
        self.assertEqual(json.loads(gzip.decompress(stub.store[key])), MONTH_DATA)

    def test_username_case_shares_cache(self):
        stub = StubRedis()
        year, month = past_month()

        async def run():
            with patch_redis(stub):
                await archive_cache.get_month_archive(
                    SITE, "Player", year, month,
                    month_url("Player", year, month),
                    lambda: AsyncMock(return_value=MONTH_DATA)(),
                )
                blocked_fetch = AsyncMock(side_effect=AssertionError("cache miss for other case"))
                return await archive_cache.get_month_archive(
                    SITE, "player", year, month,
                    month_url("player", year, month),
                    lambda: blocked_fetch(),
                )

        result = asyncio.run(run())

        self.assertEqual(result, MONTH_DATA)

    def test_archives_list_cached_with_fixed_ttl(self):
        stub = StubRedis()
        archives = {"archives": ["https://api.chess.com/pub/player/player/games/2024/01"]}
        url = "https://api.chess.com/pub/player/player/games/archives"

        async def run():
            with patch_redis(stub):
                first = await archive_cache.get_archives_list(
                    SITE, "player", url, lambda: AsyncMock(return_value=archives)(),
                )
                blocked_fetch = AsyncMock(side_effect=AssertionError("cache miss on second read"))
                second = await archive_cache.get_archives_list(
                    SITE, "player", url, lambda: blocked_fetch(),
                )
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(first, archives)
        self.assertEqual(second, archives)
        self.assertEqual(stub.getex_calls, [])
        self.assertEqual(stub.set_calls[0][2], ARCHIVE_LIST_CACHE_TTL_SECONDS)

    def test_archives_list_404_negative_cached(self):
        stub = StubRedis()
        url = "https://api.chess.com/pub/player/ghost/games/archives"
        error = UpstreamHTTPError(404, url)

        async def run():
            with patch_redis(stub):
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_archives_list(
                        SITE, "ghost", url, lambda: AsyncMock(side_effect=error)(),
                    )
                blocked_fetch = AsyncMock(side_effect=AssertionError("fetch on negative cache hit"))
                with self.assertRaises(UpstreamHTTPError):
                    await archive_cache.get_archives_list(
                        SITE, "ghost", url, lambda: blocked_fetch(),
                    )

        asyncio.run(run())

        miss_key = f"archives:miss:{SITE}:ghost"
        self.assertEqual(stub.set_calls, [(miss_key, b"1", ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS)])


class ParserCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        archive_cache._inflight.clear()

    def test_get_month_games_hits_cache_on_second_call(self):
        stub = StubRedis()
        year, month = past_month()
        get_json = AsyncMock(return_value=MONTH_DATA)

        async def run():
            with patch("parsers.chesscom.get_json", new=get_json):
                with patch_redis(stub):
                    first = await ChessComParser().get_month_games("player", year, month)
                    second = await ChessComParser().get_month_games("player", year, month)
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(first, second)
        self.assertEqual(get_json.await_count, 1)

    def test_get_last_games_hits_cache_on_second_call(self):
        stub = StubRedis()
        year, month = past_month()
        month_archive_url = month_url("player", year, month)

        async def route(url):
            if url.endswith("/games/archives"):
                return {"archives": [month_archive_url]}
            return MONTH_DATA

        get_json = AsyncMock(side_effect=route)

        async def run():
            with patch("parsers.chesscom.get_json", new=get_json):
                with patch_redis(stub):
                    parser = ChessComParser()
                    first = await parser.get_last_games("player", limit=1)
                    second = await parser.get_last_games("player", limit=1)
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        self.assertEqual(get_json.await_count, 2)

    def test_get_games_in_period_deduplicates_same_month(self):
        stub = StubRedis()
        year, month = past_month()
        start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(year, month, 28, tzinfo=timezone.utc).timestamp())
        get_json = AsyncMock(return_value=MONTH_DATA)

        async def run():
            with patch("parsers.chesscom.get_json", new=get_json):
                with patch_redis(stub):
                    return await ChessComParser().get_games_in_period("player", start, end)

        games = asyncio.run(run())

        self.assertEqual(games, [])
        self.assertEqual(get_json.await_count, 1)

    def test_get_games_in_period_current_month_uses_short_cache(self):
        stub = StubRedis()
        year, month = current_month()
        start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
        end = start + 86400
        get_json = AsyncMock(return_value=MONTH_DATA)

        async def run():
            with patch("parsers.chesscom.get_json", new=get_json):
                with patch_redis(stub):
                    parser = ChessComParser()
                    await parser.get_games_in_period("player", start, end)
                    await parser.get_games_in_period("player", start, end)
                    return parser

        asyncio.run(run())

        self.assertEqual(get_json.await_count, 1)
        key = f"archive:{SITE}:player:{year:04d}:{month:02d}"
        self.assertEqual(stub.set_calls[0][2], ARCHIVE_CURRENT_MONTH_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from db import Base, RequestEvent
from middleware import StatsMiddleware, client_ip, hash_ip
from middleware.stats import detect_source, parse_site_username
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from services import (
    cleanup_old_events,
    get_analysis_stats,
    get_daily_series,
    get_endpoint_breakdown,
    get_recent_requests,
    get_summary,
    get_top_stats,
    resolve_period,
)
from utils.visitor_names import visitor_name


def make_scope(path: str, xff: str | None = None) -> dict:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    return {"type": "http", "path": path, "root_path": "", "headers": headers, "client": ("9.9.9.9", 12345)}


class ClientIpTests(unittest.TestCase):
    def test_takes_rightmost_forwarded_entry(self):
        # The leftmost entries are client-controlled; our nginx appends the
        # real client IP, so the rightmost one is the only trusted value.
        request = Request(make_scope("/", "1.2.3.4, 5.6.7.8"))
        self.assertEqual(client_ip(request), "5.6.7.8")

    def test_falls_back_to_client_host(self):
        request = Request(make_scope("/"))
        self.assertEqual(client_ip(request), "9.9.9.9")

    def test_returns_none_without_client(self):
        scope = make_scope("/")
        scope["client"] = None
        self.assertIsNone(client_ip(Request(scope)))


class HashIpTests(unittest.TestCase):
    def test_is_deterministic_within_a_day(self):
        self.assertEqual(hash_ip("1.2.3.4", "salt"), hash_ip("1.2.3.4", "salt"))

    def test_differs_across_ips_salts_and_days(self):
        base = hash_ip("1.2.3.4", "salt")
        self.assertNotEqual(base, hash_ip("5.6.7.8", "salt"))
        self.assertNotEqual(base, hash_ip("1.2.3.4", "pepper"))

    def test_does_not_leak_the_ip(self):
        digest = hash_ip("1.2.3.4", "salt")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("1.2.3.4", digest)


class StatsMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        loop = asyncio.new_event_loop()
        self.loop = loop
        loop.run_until_complete(self._create_tables())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def _create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def tearDown(self):
        self.loop.run_until_complete(self.engine.dispose())
        self.loop.close()

    def build_app(self):
        app = FastAPI()
        app.add_middleware(StatsMiddleware)

        @app.get("/profile/{site}/{username}")
        def profile(site: str, username: str):
            return {"site": site, "username": username}

        @app.get("/ping")
        def ping():
            return {"message": "pong!"}

        @app.get("/boom")
        def boom():
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Something exploded"}, status_code=500)

        @app.get("/stream-boom")
        def stream_boom():
            from fastapi.responses import StreamingResponse

            def gen():
                yield b'{"detail": '
                yield b'"stream failed"}'

            return StreamingResponse(gen(), status_code=502, media_type="application/json")

        return app

    def test_records_template_and_excluded_paths_are_skipped(self):
        captured = []

        with (
            patch("middleware.stats.DATABASE_URL", "sqlite+aiosqlite://"),
            patch.object(StatsMiddleware, "_record", lambda self, event: captured.append(event)),
        ):
            app = self.build_app()
            with TestClient(app) as client:
                client.get("/profile/chesscom/magnus")
                client.get("/ping")
                client.get("/definitely/missing")

        endpoints = [e["endpoint"] for e in captured]
        self.assertEqual(endpoints, ["/profile/{site}/{username}", "<unmatched>"])
        self.assertEqual([e["path"] for e in captured], ["/profile/chesscom/magnus", "/definitely/missing"])
        self.assertEqual([e["method"] for e in captured], ["GET", "GET"])
        self.assertIn("ip_hash", captured[0])

    def test_error_detail_is_captured_without_breaking_response(self):
        captured = []

        with (
            patch("middleware.stats.DATABASE_URL", "sqlite+aiosqlite://"),
            patch.object(StatsMiddleware, "_record", lambda self, event: captured.append(event)),
        ):
            app = self.build_app()
            with TestClient(app) as client:
                boom = client.get("/boom")
                stream = client.get("/stream-boom")
                ok = client.get("/profile/chesscom/magnus")

        # Clients still receive the full original bodies.
        self.assertEqual(boom.status_code, 500)
        self.assertEqual(boom.json(), {"detail": "Something exploded"})
        self.assertEqual(stream.status_code, 502)
        self.assertEqual(stream.json(), {"detail": "stream failed"})
        self.assertEqual(ok.status_code, 200)

        by_path = {e["path"]: e for e in captured}
        self.assertEqual(by_path["/boom"].get("error_detail"), "Something exploded")
        self.assertEqual(by_path["/stream-boom"].get("error_detail"), "stream failed")
        self.assertNotIn("error_detail", by_path["/profile/chesscom/magnus"])

    def test_long_error_body_is_truncated(self):
        async def run():
            from starlette.responses import JSONResponse

            response = JSONResponse({"detail": "x" * 5000}, status_code=400)
            return await StatsMiddleware._read_error_detail(response)

        detail = self.loop.run_until_complete(run())
        self.assertEqual(len(detail), 1000)

    def test_insert_writes_a_row(self):
        async def run():
            with (
                patch("middleware.stats.DATABASE_URL", "sqlite+aiosqlite://"),
                patch("middleware.stats.get_session_factory", return_value=self.factory),
            ):
                middleware = StatsMiddleware(FastAPI())
                await middleware._insert(
                    {
                        "path": "/analyze/pgn",
                        "endpoint": "/analyze/pgn",
                        "method": "POST",
                        "status": 200,
                        "duration_ms": 120,
                        "ip_hash": "a" * 64,
                        "cache_hit": True,
                        "moves_count": 40,
                    }
                )
            await self._assert_single_row()

        self.loop.run_until_complete(run())

    async def _assert_single_row(self):
        async with self.factory() as session:
            rows = (await session.execute(RequestEvent.__table__.select())).fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.endpoint, "/analyze/pgn")
            self.assertEqual(row.status, 200)
            self.assertEqual(row.duration_ms, 120)
            self.assertTrue(row.cache_hit)
            self.assertEqual(row.moves_count, 40)


class StatsAggregationTests(unittest.TestCase):
    DAYS = 7

    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._create_tables())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.loop.run_until_complete(self._seed())

    async def _create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _seed(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)
        events = [
            RequestEvent(ts=now, path="/profile/chesscom/player", endpoint="/profile/{site}/{username}", method="GET", status=200, duration_ms=100, ip_hash="a" * 64),
            RequestEvent(ts=now, path="/missing", endpoint="<unmatched>", method="GET", status=404, duration_ms=200, ip_hash="a" * 64),
            RequestEvent(ts=now, path="/analyze/pgn", endpoint="/analyze/pgn", method="POST", status=200, duration_ms=300, ip_hash="b" * 64, cache_hit=True, moves_count=40),
            RequestEvent(ts=now, path="/analyze/pgn", endpoint="/analyze/pgn", method="POST", status=200, duration_ms=500, ip_hash="b" * 64, cache_hit=False, moves_count=20),
            RequestEvent(ts=old, path="/games/page/chesscom/player", endpoint="/games/page/{site}/{username}", method="GET", status=200, duration_ms=150, ip_hash="c" * 64),
        ]
        async with self.factory() as session:
            session.add_all(events)
            await session.commit()

    def tearDown(self):
        self.loop.run_until_complete(self.engine.dispose())
        self.loop.close()

    def test_summary(self):
        async def run():
            async with self.factory() as session:
                return await get_summary(session, self.DAYS)

        summary = self.loop.run_until_complete(run())
        self.assertEqual(summary["requests"], 4)
        self.assertEqual(summary["unique_visitors"], 2)
        # The seeded 404 (player not found / unmatched endpoint) is a correct
        # backend answer, not an error — only 5xx count.
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["avg_duration_ms"], 275)

    def test_daily_series(self):
        async def run():
            async with self.factory() as session:
                return await get_daily_series(session, self.DAYS)

        series = self.loop.run_until_complete(run())
        # Gaps are filled so the chart has no holes: 7 days, 7 points.
        self.assertEqual(len(series), 7)
        self.assertEqual(sum(r["requests"] for r in series), 4)
        today = series[-1]
        self.assertEqual(today["requests"], 4)
        self.assertEqual(today["analyses"], 2)
        self.assertEqual(today["unique_visitors"], 2)
        self.assertEqual(today["errors"], 0)
        # No bot traffic in this fixture yet.
        self.assertEqual(today["bots"], 0)

    def test_daily_series_counts_bots_separately(self):
        async def run():
            async with self.factory() as session:
                now = datetime.now(timezone.utc)
                session.add_all(
                    [
                        RequestEvent(ts=now, path="/games/page/chesscom/player", endpoint="/games/page/{site}/{username}", method="GET", status=200, duration_ms=100, ip_hash="e" * 64, source="bot"),
                        RequestEvent(ts=now, path="/analyze/pgn", endpoint="/analyze/pgn", method="POST", status=200, duration_ms=100, ip_hash="e" * 64, source="bot"),
                    ]
                )
                await session.commit()
                return await get_daily_series(session, self.DAYS)

        series = self.loop.run_until_complete(run())
        today = series[-1]
        self.assertEqual(today["requests"], 6)
        self.assertEqual(today["bots"], 2)
        # Bots stay inside the total; the chart splits them as a layer.
        self.assertLessEqual(today["bots"], today["requests"])

    def test_endpoint_breakdown_is_sorted_by_requests(self):
        async def run():
            async with self.factory() as session:
                return await get_endpoint_breakdown(session, self.DAYS)

        rows = self.loop.run_until_complete(run())
        by_endpoint = {r["endpoint"]: r for r in rows}
        self.assertEqual(
            set(by_endpoint),
            {"/profile/{site}/{username}", "/analyze/pgn", "<unmatched>"},
        )
        # Ties are unordered, but the list itself must be sorted descending.
        requests = [r["requests"] for r in rows]
        self.assertEqual(requests, sorted(requests, reverse=True))
        analyze_row = by_endpoint["/analyze/pgn"]
        self.assertEqual(analyze_row["requests"], 2)
        self.assertEqual(analyze_row["errors"], 0)
        self.assertEqual(analyze_row["avg_duration_ms"], 400)
        # The unmatched 404 is not an error either.
        self.assertEqual(by_endpoint["<unmatched>"]["errors"], 0)

    def test_only_server_errors_count(self):
        async def run():
            async with self.factory() as session:
                now = datetime.now(timezone.utc)
                session.add_all(
                    [
                        RequestEvent(ts=now, path="/analyze/pgn", endpoint="/analyze/pgn", method="POST", status=500, duration_ms=100, ip_hash="d" * 64),
                        RequestEvent(ts=now, path="/analyze/pgn", endpoint="/analyze/pgn", method="POST", status=422, duration_ms=100, ip_hash="d" * 64),
                        RequestEvent(ts=now, path="/probe", endpoint="<unmatched>", method="GET", status=429, duration_ms=100, ip_hash="d" * 64),
                    ]
                )
                await session.commit()
                summary = await get_summary(session, self.DAYS)
                series = await get_daily_series(session, self.DAYS)
                breakdown = await get_endpoint_breakdown(session, self.DAYS)
                return summary, series, breakdown

        summary, series, breakdown = self.loop.run_until_complete(run())
        # Only the 500 counts; 404 (seed) / 422 / 429 are correct answers.
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(series[-1]["errors"], 1)
        by_endpoint = {r["endpoint"]: r for r in breakdown}
        self.assertEqual(by_endpoint["/analyze/pgn"]["errors"], 1)
        self.assertEqual(by_endpoint["<unmatched>"]["errors"], 0)

    def test_analysis_stats(self):
        async def run():
            async with self.factory() as session:
                return await get_analysis_stats(session, self.DAYS)

        stats = self.loop.run_until_complete(run())
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["cache_hit_rate"], 0.5)
        self.assertEqual(stats["avg_moves"], 30.0)

    def test_recent_requests_include_paths_and_are_newest_first(self):
        async def run():
            async with self.factory() as session:
                return await get_recent_requests(session, self.DAYS)

        rows = self.loop.run_until_complete(run())
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["path"], "/analyze/pgn")
        self.assertEqual(rows[1]["path"], "/analyze/pgn")
        self.assertEqual(rows[2]["path"], "/missing")
        self.assertEqual(rows[2]["status"], 404)

    def test_recent_requests_pagination(self):
        async def run(limit, offset):
            async with self.factory() as session:
                return await get_recent_requests(session, self.DAYS, limit=limit, offset=offset)

        # Seed durations, newest first: 500, 300, 200, 100.
        page1 = self.loop.run_until_complete(run(2, 0))
        page2 = self.loop.run_until_complete(run(2, 2))
        self.assertEqual([r["duration_ms"] for r in page1], [500, 300])
        self.assertEqual([r["duration_ms"] for r in page2], [200, 100])
        self.assertEqual(self.loop.run_until_complete(run(2, 4)), [])

    def test_cleanup_deletes_only_old_events(self):
        async def run():
            async with self.factory() as session:
                deleted = await cleanup_old_events(session, self.DAYS)
                remaining = (await session.execute(RequestEvent.__table__.select())).fetchall()
                return deleted, len(remaining)

        deleted, remaining = self.loop.run_until_complete(run())
        self.assertEqual(deleted, 1)
        self.assertEqual(remaining, 4)


class SourceDetectionTests(unittest.TestCase):
    def _request(self, path="/profile/chesscom/magnus", headers=None) -> Request:
        raw = []
        for k, v in (headers or {}).items():
            raw.append((k.lower().encode(), v.encode()))
        return Request({"type": "http", "path": path, "headers": raw, "client": ("9.9.9.9", 1)})

    def test_frontend_header_plus_origin(self):
        req = self._request(headers={"user-agent": "Mozilla/5.0", "x-frontend": "1", "origin": "https://chess-stat.ru"})
        with patch("middleware.stats.CORS_ORIGINS", ("https://chess-stat.ru",)):
            self.assertEqual(detect_source(req), "frontend")

    def test_header_without_origin_is_other(self):
        req = self._request(headers={"user-agent": "Mozilla/5.0", "x-frontend": "1"})
        with patch("middleware.stats.CORS_ORIGINS", ("https://chess-stat.ru",)):
            self.assertEqual(detect_source(req), "other")

    def test_bot_user_agent(self):
        req = self._request(headers={"user-agent": "curl/8.0"})
        self.assertEqual(detect_source(req), "bot")

    def test_plain_browser_is_other(self):
        req = self._request(headers={"user-agent": "Mozilla/5.0"})
        self.assertEqual(detect_source(req), "other")

    def test_scanner_paths_are_bot_despite_browser_ua(self):
        browser = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        for path in (
            "/.env",
            "/v1/.env",
            "/staging/.env",
            "/dev/.env",
            "/env",
            "/v1/env",
            "/v1/config",
            "/v1/settings",
            "/config",
            "/settings",
            "/aws.json",
            "/graphql",
        ):
            with self.subTest(path=path):
                req = self._request(path=path, headers={"user-agent": browser})
                self.assertEqual(detect_source(req), "bot")

    def test_missing_user_agent_is_bot(self):
        self.assertEqual(detect_source(self._request(headers={})), "bot")

    def test_player_named_like_probe_is_not_bot(self):
        req = self._request(
            path="/profile/chesscom/env",
            headers={"user-agent": "Mozilla/5.0"},
        )
        self.assertEqual(detect_source(req), "other")


class SiteUsernameParsingTests(unittest.TestCase):
    def test_profile(self):
        self.assertEqual(
            parse_site_username("/profile/chesscom/Magnus"),
            ("chesscom", "magnus"),
        )

    def test_games_page(self):
        self.assertEqual(
            parse_site_username("/games/page/chesscom/Hikaru"),
            ("chesscom", "hikaru"),
        )

    def test_no_match(self):
        self.assertEqual(parse_site_username("/analyze/pgn"), (None, None))
        self.assertEqual(parse_site_username("/ping"), (None, None))


class PeriodTests(unittest.TestCase):
    def test_24h_and_today(self):
        name, since, gran = resolve_period("24h", None)
        self.assertEqual((name, gran), ("24h", "hour"))
        self.assertTrue(datetime.now(timezone.utc) - since < timedelta(hours=25))
        name, since, gran = resolve_period("today", None)
        self.assertEqual((name, gran), ("today", "hour"))
        self.assertEqual(since.hour, 0)

    def test_year_period(self):
        name, since, gran = resolve_period("1y", None)
        self.assertEqual((name, gran), ("1y", "day"))
        self.assertTrue(
            timedelta(days=364) < datetime.now(timezone.utc) - since < timedelta(days=366)
        )

    def test_days_fallback(self):
        name, _, gran = resolve_period(None, 7)
        self.assertEqual((name, gran), ("7d", "day"))
        name, since, gran = resolve_period(None, 10)
        self.assertEqual(name, "10d")
        self.assertTrue(datetime.now(timezone.utc) - since < timedelta(days=11))

    def test_long_ranges_are_chunked(self):
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        async def run():
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add_all(
                    [
                        RequestEvent(
                            ts=now - timedelta(days=d),
                            path="/profile/chesscom/player",
                            endpoint="/profile/{site}/{username}",
                            method="GET", status=200, duration_ms=10,
                            ip_hash="a" * 64,
                        )
                        for d in range(90)
                    ]
                )
                await session.commit()
                weekly = await get_daily_series(session, 30, period="90d")
                monthly = await get_daily_series(session, 30, period="1y")
            await engine.dispose()
            return weekly, monthly

        weekly, monthly = asyncio.new_event_loop().run_until_complete(run())
        # 90 days in 7-day chunks, 365 days in 30-day chunks.
        self.assertEqual(len(weekly), 13)
        self.assertEqual(sum(p["requests"] for p in weekly), 90)
        self.assertEqual(len(monthly), 13)
        self.assertEqual(sum(p["requests"] for p in monthly), 90)


class VisitorNameTests(unittest.TestCase):
    def test_deterministic_and_shaped(self):
        first = visitor_name("a" * 64)
        self.assertEqual(first, visitor_name("a" * 64))
        self.assertRegex(first, r"^[a-z]+-[a-z]+-\d{2}$")
        self.assertNotEqual(first, visitor_name("b" * 64))
        self.assertIsNone(visitor_name(None))


class TopStatsTests(unittest.TestCase):
    def test_groups_by_site_and_username(self):
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        async def run():
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add_all(
                    [
                        RequestEvent(
                            ts=now, path="/profile/chesscom/magnus", endpoint="/profile/{site}/{username}",
                            method="GET", status=200, duration_ms=10, ip_hash="a" * 64,
                            source="frontend", site="chesscom", username="magnus",
                        ),
                        RequestEvent(
                            ts=now, path="/profile/chesscom/hikaru", endpoint="/profile/{site}/{username}",
                            method="GET", status=200, duration_ms=10, ip_hash="b" * 64,
                            source="other", site="chesscom", username="hikaru",
                        ),
                        RequestEvent(
                            ts=now, path="/analyze/pgn", endpoint="/analyze/pgn",
                            method="POST", status=200, duration_ms=10, ip_hash="a" * 64,
                            source="frontend",
                        ),
                    ]
                )
                await session.commit()
                top_all = await get_top_stats(session, 7)
                top_filtered = await get_top_stats(session, 7, endpoints=["/analyze/pgn"])
            await engine.dispose()
            return top_all, top_filtered

        top_all, top_filtered = asyncio.new_event_loop().run_until_complete(run())
        self.assertEqual(len(top_all["sites"]), 1)
        self.assertEqual(top_all["sites"][0]["name"], "chesscom")
        self.assertEqual(top_all["sites"][0]["requests"], 2)
        self.assertEqual(
            {u["name"] for u in top_all["usernames"]}, {"magnus", "hikaru"}
        )
        self.assertEqual(top_filtered["sites"], [])
        self.assertEqual(top_filtered["usernames"], [])


class AnalysisBreakdownTests(unittest.TestCase):
    def test_local_cache_split(self):
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        async def run():
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add_all(
                    [
                        RequestEvent(
                            ts=now, path="/analyze/pgn", endpoint="/analyze/pgn",
                            method="POST", status=200, duration_ms=10, ip_hash="a" * 64,
                            cache_hit=False, moves_count=20,
                        ),
                        RequestEvent(
                            ts=now, path="/analyze/pgn", endpoint="/analyze/pgn",
                            method="POST", status=200, duration_ms=10, ip_hash="b" * 64,
                            cache_hit=True, moves_count=None,
                        ),
                    ]
                )
                await session.commit()
                stats = await get_analysis_stats(session, 7)
            await engine.dispose()
            return stats

        stats = asyncio.new_event_loop().run_until_complete(run())
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["local"], 1)
        self.assertEqual(stats["cache"], 1)
        self.assertEqual(stats["local_moves"], 20)
        self.assertEqual(stats["avg_local_moves"], 20.0)


if __name__ == "__main__":
    unittest.main()

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session_factory
from db.models import RequestEvent
from utils.visitor_names import visitor_name

logger = logging.getLogger(__name__)

ANALYZE_ENDPOINT_PATTERN = "/analyze%"
VALID_PERIODS = ("today", "24h", "7d", "30d", "90d", "1y")
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
RETENTION_INTERVAL_SECONDS = 6 * 3600


def resolve_period(period: str | None, days: int | None = None) -> tuple[str, datetime, str]:
    """Return (period_name, since, granularity) where granularity is hour|day."""
    name = (period or "").strip().lower()
    now = datetime.now(timezone.utc)
    if name in VALID_PERIODS:
        pass
    elif days is not None:
        # Deprecated ?days=N path: honor arbitrary N instead of snapping.
        return f"{days}d", now - timedelta(days=days), "day"
    else:
        name = "30d"
    if name == "24h":
        return name, now - timedelta(hours=24), "hour"
    if name == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return name, since, "hour"
    return name, now - timedelta(days=PERIOD_DAYS.get(name, 30)), "day"


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _with_visitor(rows: list[dict], hash_key: str = "ip_hash") -> list[dict]:
    for row in rows:
        h = row.pop(hash_key, None)
        row["visitor_name"] = visitor_name(h)
        if h is not None:
            row["ip_hash"] = h
    return rows


async def get_summary(session: AsyncSession, days: int, period: str | None = None) -> dict:
    name, since, _ = resolve_period(period, days)
    requests, unique_visitors, errors, avg_ms, frontend, other = (
        await session.execute(
            select(
                func.count(),
                func.count(func.distinct(RequestEvent.ip_hash)),
                # Only 5xx count as errors: 4xx (unknown player, unmatched
                # endpoint, bad payload, rate limit) means the backend worked
                # as intended. Recent rows still expose every status as-is.
                func.count().filter(RequestEvent.status >= 500),
                func.avg(RequestEvent.duration_ms),
                func.count().filter(RequestEvent.source == "frontend"),
                func.count().filter(RequestEvent.source != "frontend"),
            ).where(RequestEvent.ts >= since)
        )
    ).one()
    return {
        "requests": requests,
        "unique_visitors": unique_visitors,
        "errors": errors,
        "avg_duration_ms": round(avg_ms) if avg_ms is not None else None,
        "frontend": frontend,
        "other": other,
        "period": name,
    }


async def get_daily_series(
    session: AsyncSession, days: int, period: str | None = None
) -> list[dict]:
    name, since, granularity = resolve_period(period, days)
    if granularity == "hour":
        # func.strftime is SQLite-only and date_trunc is Postgres-only;
        # branch on the session dialect so both work.
        try:
            dialect = session.get_bind().dialect.name
        except Exception:
            dialect = ""
        if dialect.startswith("postgres"):
            bucket = func.to_char(RequestEvent.ts, 'YYYY-MM-DD"T"HH24:00').label("bucket")
        else:
            bucket = func.strftime("%Y-%m-%dT%H:00", RequestEvent.ts).label("bucket")
    else:
        bucket = func.date(RequestEvent.ts).label("bucket")
    rows = (
        await session.execute(
            select(
                bucket,
                func.count(),
                func.count().filter(RequestEvent.endpoint.like(ANALYZE_ENDPOINT_PATTERN)),
                func.count(func.distinct(RequestEvent.ip_hash)),
                func.count().filter(RequestEvent.status >= 500),
                func.count().filter(RequestEvent.source == "bot"),
            )
            .where(RequestEvent.ts >= since)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    by_bucket = {str(r[0]): r for r in rows}
    # Fill gaps so the chart has no holes.
    filled: list[dict] = []
    if granularity == "hour":
        now = datetime.now(timezone.utc)
        if name == "24h":
            # All hour buckets intersecting the last 24h window.
            start = (since.replace(minute=0, second=0, microsecond=0)).replace(
                tzinfo=None
            )
            end = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
            keys = []
            cursor = start
            while cursor <= end:
                keys.append(cursor.strftime("%Y-%m-%dT%H:00"))
                cursor += timedelta(hours=1)
        else:  # today: 00:00 UTC .. current hour
            base = since.replace(tzinfo=None)
            keys = [
                (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00")
                for i in range(now.hour + 1)
            ]
        for key in keys:
            r = by_bucket.get(key)
            filled.append(
                {
                    "date": key,
                    "requests": r[1] if r else 0,
                    "analyses": r[2] if r else 0,
                    "unique_visitors": r[3] if r else 0,
                    "errors": r[4] if r else 0,
                    "bots": r[5] if r else 0,
                }
            )
    else:
        total_days = PERIOD_DAYS.get(name, days or 30)
        base = datetime.now(timezone.utc).date()
        for i in range(total_days):
            key = str(base - timedelta(days=total_days - 1 - i))
            r = by_bucket.get(key)
            filled.append(
                {
                    "date": key,
                    "requests": r[1] if r else 0,
                    "analyses": r[2] if r else 0,
                    "unique_visitors": r[3] if r else 0,
                    "errors": r[4] if r else 0,
                    "bots": r[5] if r else 0,
                }
            )
        # Long ranges are bucketed coarser so the chart stays readable:
        # weekly points for ~90d, monthly for ~1y. Uniques are summed as
        # visitor-days, consistent with the daily semantics everywhere else.
        chunk = 1 if total_days <= 30 else (7 if total_days <= 120 else 30)
        if chunk > 1:
            merged: list[dict] = []
            for i in range(0, len(filled), chunk):
                part = filled[i : i + chunk]
                merged.append(
                    {
                        "date": part[0]["date"],
                        "requests": sum(p["requests"] for p in part),
                        "analyses": sum(p["analyses"] for p in part),
                        "unique_visitors": sum(p["unique_visitors"] for p in part),
                        "errors": sum(p["errors"] for p in part),
                        "bots": sum(p["bots"] for p in part),
                    }
                )
            return merged
    return filled


async def get_endpoint_breakdown(
    session: AsyncSession, days: int, period: str | None = None
) -> list[dict]:
    _, since, _ = resolve_period(period, days)
    requests = func.count().label("requests")
    rows = (
        await session.execute(
            select(
                RequestEvent.endpoint,
                requests,
                func.count().filter(RequestEvent.status >= 500).label("errors"),
                func.avg(RequestEvent.duration_ms).label("avg_duration_ms"),
                func.count(func.distinct(RequestEvent.ip_hash)).label("unique_visitors"),
                func.count().filter(RequestEvent.source == "frontend").label("frontend"),
                func.count().filter(RequestEvent.source != "frontend").label("other"),
            )
            .where(RequestEvent.ts >= since)
            .group_by(RequestEvent.endpoint)
            .order_by(desc(requests))
        )
    ).all()
    return [
        {
            "endpoint": row.endpoint,
            "requests": row.requests,
            "errors": row.errors,
            "avg_duration_ms": round(row.avg_duration_ms) if row.avg_duration_ms is not None else None,
            "unique_visitors": row.unique_visitors,
            "frontend": row.frontend,
            "other": row.other,
        }
        for row in rows
    ]


async def get_recent_requests(
    session: AsyncSession,
    days: int,
    limit: int = 100,
    period: str | None = None,
    visitor_hash: str | None = None,
    offset: int = 0,
) -> list[dict]:
    _, since, _ = resolve_period(period, days)
    stmt = select(RequestEvent).where(RequestEvent.ts >= since)
    if visitor_hash:
        stmt = stmt.where(RequestEvent.ip_hash == visitor_hash)
    rows = (
        await session.execute(
            stmt.order_by(desc(RequestEvent.ts), desc(RequestEvent.id))
            .limit(max(limit, 0))
            .offset(max(offset, 0))
        )
    ).scalars().all()
    result = [
        {
            "ts": row.ts,
            "method": row.method,
            "path": row.path,
            "endpoint": row.endpoint,
            "status": row.status,
            "duration_ms": row.duration_ms,
            "source": getattr(row, "source", "other") or "other",
            "site": row.site,
            "username": row.username,
            "cache_hit": row.cache_hit,
            "moves_count": row.moves_count,
            "ip_hash": row.ip_hash,
            "error_detail": row.error_detail,
        }
        for row in rows
    ]
    return _with_visitor(result)


async def get_analysis_stats(
    session: AsyncSession, days: int, period: str | None = None
) -> dict:
    _, since, _ = resolve_period(period, days)
    base = RequestEvent.ts >= since, RequestEvent.endpoint.like(ANALYZE_ENDPOINT_PATTERN)
    total, cache_hits, avg_moves = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(RequestEvent.cache_hit.is_(True)),
                func.avg(RequestEvent.moves_count),
            ).where(*base)
        )
    ).one()
    local_total, local_moves = (
        await session.execute(
            select(
                func.count(),
                func.sum(RequestEvent.moves_count),
            ).where(*base, RequestEvent.cache_hit.is_not(True))
        )
    ).one()
    cache_total, cache_moves = (
        await session.execute(
            select(
                func.count(),
                func.sum(RequestEvent.moves_count),
            ).where(*base, RequestEvent.cache_hit.is_(True))
        )
    ).one()
    local_moves = int(local_moves or 0)
    cache_moves = int(cache_moves or 0)
    return {
        "total": total,
        "cache_hit_rate": round(cache_hits / total, 4) if total else 0.0,
        "avg_moves": round(avg_moves, 1) if avg_moves is not None else None,
        "local": local_total,
        "cache": cache_total,
        "local_moves": local_moves,
        "cache_moves": cache_moves,
        "avg_local_moves": round(local_moves / local_total, 1) if local_total else None,
    }


async def get_top_stats(
    session: AsyncSession,
    days: int,
    period: str | None = None,
    endpoints: list[str] | None = None,
    limit: int = 20,
) -> dict:
    _, since, _ = resolve_period(period, days)
    conds = [RequestEvent.ts >= since, RequestEvent.site.is_not(None)]
    if endpoints:
        conds.append(RequestEvent.endpoint.in_(endpoints))
    site_rows = (
        await session.execute(
            select(
                RequestEvent.site,
                func.count().label("requests"),
                func.count(func.distinct(RequestEvent.ip_hash)).label("unique_visitors"),
            )
            .where(*conds)
            .group_by(RequestEvent.site)
            .order_by(desc("requests"))
            .limit(limit)
        )
    ).all()
    user_conds = [RequestEvent.ts >= since, RequestEvent.username.is_not(None)]
    if endpoints:
        user_conds.append(RequestEvent.endpoint.in_(endpoints))
    user_rows = (
        await session.execute(
            select(
                RequestEvent.username,
                func.count().label("requests"),
                func.count(func.distinct(RequestEvent.ip_hash)).label("unique_visitors"),
            )
            .where(*user_conds)
            .group_by(RequestEvent.username)
            .order_by(desc("requests"))
            .limit(limit)
        )
    ).all()
    return {
        "sites": [
            {"name": r.site, "requests": r.requests, "unique_visitors": r.unique_visitors}
            for r in site_rows
        ],
        "usernames": [
            {"name": r.username, "requests": r.requests, "unique_visitors": r.unique_visitors}
            for r in user_rows
        ],
    }


async def cleanup_old_events(session: AsyncSession, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await session.execute(delete(RequestEvent).where(RequestEvent.ts < cutoff))
    await session.commit()
    return result.rowcount or 0


async def retention_loop(retention_days: int, interval_seconds: int = RETENTION_INTERVAL_SECONDS) -> None:
    """Periodically delete events older than the retention window."""
    while True:
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                deleted = await cleanup_old_events(session, retention_days)
            if deleted:
                logger.info(
                    "Stats retention: deleted %d events older than %d days",
                    deleted,
                    retention_days,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Stats retention cleanup failed", exc_info=True)
        await asyncio.sleep(interval_seconds)

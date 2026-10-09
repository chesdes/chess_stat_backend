import asyncio
import gzip
import json
import logging
from datetime import datetime, timezone

from config import (
    ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS,
    ARCHIVE_CACHE_TTL_SECONDS,
    ARCHIVE_CURRENT_MONTH_TTL_SECONDS,
    ARCHIVE_LIST_CACHE_TTL_SECONDS,
)
from .http_client import UpstreamHTTPError
from .redis_client import RedisClient

logger = logging.getLogger(__name__)

_inflight: dict[str, asyncio.Task] = {}


def _month_key(site: str, username: str, year: int, month: int) -> str:
    return f"archive:{site}:{username.lower()}:{year:04d}:{month:02d}"


def _month_miss_key(site: str, username: str, year: int, month: int) -> str:
    return f"archive:miss:{site}:{username.lower()}:{year:04d}:{month:02d}"


def _list_key(site: str, username: str) -> str:
    return f"archives:{site}:{username.lower()}"


def _list_miss_key(site: str, username: str) -> str:
    return f"archives:miss:{site}:{username.lower()}"


def _is_current_month(year: int, month: int) -> bool:
    now = datetime.now(timezone.utc)
    return year == now.year and month == now.month


def _encode(data) -> bytes:
    return gzip.compress(json.dumps(data).encode())


def _decode(raw: bytes):
    return json.loads(gzip.decompress(raw))


async def _dedup_fetch(key: str, fetch):
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(fetch())
        _inflight[key] = task
    try:
        return await task
    finally:
        if _inflight.get(key) is task:
            _inflight.pop(key, None)


async def _cached_get_json(key: str, miss_key: str | None, ttl: int, refresh_on_read: bool, url: str, fetch):
    redis = None
    try:
        redis = RedisClient.get_client()
    except Exception:
        redis = None

    if redis is not None:
        try:
            raw = await (redis.getex(key, ex=ttl) if refresh_on_read else redis.get(key))
            if raw:
                logger.debug("Archive cache hit: %s", key)
                return _decode(raw)
            if miss_key and await redis.get(miss_key):
                raise UpstreamHTTPError(404, url)
        except UpstreamHTTPError:
            raise
        except Exception:
            logger.warning("Archive cache read failed: %s", key, exc_info=True)

    try:
        data = await _dedup_fetch(key, fetch)
    except UpstreamHTTPError as e:
        if e.status_code == 404 and miss_key and redis is not None:
            try:
                await redis.set(miss_key, b"1", ex=ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS)
            except Exception:
                logger.warning("Archive negative cache write failed: %s", miss_key, exc_info=True)
        raise

    if redis is not None:
        try:
            await redis.set(key, _encode(data), ex=ttl)
        except Exception:
            logger.warning("Archive cache write failed: %s", key, exc_info=True)
    return data


async def get_month_archive(site: str, username: str, year: int, month: int, url: str, fetch):
    """Monthly archive JSON. Past-month archives are immutable on chess.com,
    so they are cached with a sliding TTL refreshed on every read; the current
    month gets a short fixed TTL instead."""
    current = _is_current_month(year, month)
    return await _cached_get_json(
        key=_month_key(site, username, year, month),
        miss_key=None if current else _month_miss_key(site, username, year, month),
        ttl=ARCHIVE_CURRENT_MONTH_TTL_SECONDS if current else ARCHIVE_CACHE_TTL_SECONDS,
        refresh_on_read=not current,
        url=url,
        fetch=fetch,
    )


async def get_archives_list(site: str, username: str, url: str, fetch):
    """List of monthly archive URLs. The list only changes when a new month
    appears, so a short fixed TTL keeps staleness bounded without refresh."""
    return await _cached_get_json(
        key=_list_key(site, username),
        miss_key=_list_miss_key(site, username),
        ttl=ARCHIVE_LIST_CACHE_TTL_SECONDS,
        refresh_on_read=False,
        url=url,
        fetch=fetch,
    )
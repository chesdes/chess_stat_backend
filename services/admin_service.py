import secrets
from datetime import datetime, timedelta, timezone

import jwt

from config import (
    ADMIN_BAN_SECONDS,
    ADMIN_MAX_ATTEMPTS,
    ADMIN_PASSWORD,
    ADMIN_SECRET,
    ADMIN_TOKEN_TTL_SECONDS,
)
from db import get_session_factory
from services.stats_service import (
    get_analysis_stats,
    get_daily_series,
    get_endpoint_breakdown,
    get_recent_requests,
    get_summary,
    get_top_stats,
    resolve_period,
)

LOGIN_FAILED_MESSAGE = "Login failed, try again later"


class AdminNotConfiguredError(RuntimeError):
    pass


class InvalidAdminPasswordError(ValueError):
    pass


class InvalidAdminTokenError(ValueError):
    pass


class AdminBannedError(ValueError):
    pass


def _ban_key(ip: str) -> str:
    return f"admin:ban:{ip}"


def _fail_key(ip: str) -> str:
    return f"admin:fail:{ip}"


async def check_login_allowed(ip: str | None) -> None:
    """Raise AdminBannedError when the IP is currently banned."""
    if not ip:
        return
    try:
        from utils import RedisClient

        redis = RedisClient.get_client()
        if await redis.exists(_ban_key(ip)):
            raise AdminBannedError
    except AdminBannedError:
        raise
    except Exception:
        # Fail open: a Redis outage must not lock admins out.
        return


async def register_login_failure(ip: str | None) -> None:
    if not ip:
        return
    try:
        from utils import RedisClient

        redis = RedisClient.get_client()
        fails = await redis.incr(_fail_key(ip))
        if fails == 1:
            await redis.expire(_fail_key(ip), ADMIN_BAN_SECONDS)
        if fails >= ADMIN_MAX_ATTEMPTS:
            await redis.set(_ban_key(ip), "1", ex=ADMIN_BAN_SECONDS)
    except Exception:
        return


async def register_login_success(ip: str | None) -> None:
    if not ip:
        return
    try:
        from utils import RedisClient

        redis = RedisClient.get_client()
        await redis.delete(_fail_key(ip))
    except Exception:
        return


class AdminService:
    def create_token(self) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "admin",
            "iat": now,
            "exp": now + timedelta(seconds=ADMIN_TOKEN_TTL_SECONDS),
        }
        return jwt.encode(payload, ADMIN_SECRET, algorithm="HS256")

    def verify_token(self, token: str) -> None:
        try:
            payload = jwt.decode(
                token,
                ADMIN_SECRET,
                algorithms=["HS256"],
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidAdminTokenError from exc
        if payload.get("sub") != "admin":
            raise InvalidAdminTokenError

    def authenticate(self, password: str) -> dict:
        self._ensure_configured()
        if not secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            raise InvalidAdminPasswordError
        return {
            "token": self.create_token(),
            "expires_in": ADMIN_TOKEN_TTL_SECONDS,
        }

    def require_token(self, token: str | None) -> None:
        self._ensure_configured()
        if not token:
            raise InvalidAdminTokenError
        self.verify_token(token)

    async def get_stats(
        self,
        days: int = 30,
        period: str | None = None,
        top_endpoints: list[str] | None = None,
        visitor: str | None = None,
    ) -> dict:
        name, _, _ = resolve_period(period, days)
        session_factory = get_session_factory()
        async with session_factory() as session:
            return {
                "summary": await get_summary(session, days, period),
                "daily": await get_daily_series(session, days, period),
                "endpoints": await get_endpoint_breakdown(session, days, period),
                "recent": await get_recent_requests(
                    session, days, limit=0, period=period, visitor_hash=visitor
                ),
                "analysis": await get_analysis_stats(session, days, period),
                "top": await get_top_stats(session, days, period, top_endpoints),
                "period": name,
            }

    async def get_recent(
        self,
        days: int = 30,
        period: str | None = None,
        visitor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        session_factory = get_session_factory()
        async with session_factory() as session:
            return await get_recent_requests(
                session, days, limit=limit, period=period, visitor_hash=visitor, offset=offset
            )

    @staticmethod
    def _ensure_configured() -> None:
        if not ADMIN_PASSWORD:
            raise AdminNotConfiguredError

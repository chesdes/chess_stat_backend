import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from config import CORS_ORIGINS, DATABASE_URL, STATS_IP_SALT
from db import RequestEvent, get_session_factory
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import get_route_path

logger = logging.getLogger(__name__)

EXCLUDED_PATHS = frozenset({"/ping", "/health", "/ready"})
# /admin/stats and /admin/recent self-poll on every admin panel open and
# would flood the stats; /admin/login is intentionally logged so login
# attempts (including brute-force) are visible in recent requests.
EXCLUDED_PREFIXES = ("/admin/stats", "/admin/recent")
# Upper bound on concurrently open stats inserts; when the database is slow
# or down, extra events are dropped instead of piling up.
MAX_PENDING_EVENTS = 64

# Error response bodies are inspected only, never stored whole.
ERROR_BODY_READ_LIMIT = 2048
ERROR_DETAIL_MAX_LEN = 1000

UNMATCHED_ENDPOINT = "<unmatched>"

BOT_UA_PATTERN = re.compile(
    r"bot|crawl|spider|slurp|headless|selenium|playwright|phantomjs|"
    r"python-requests|python-urllib|urllib|curl|wget|go-http|java|axios|postman|insomnia",
    re.IGNORECASE,
)

# Scanner probes (/​.env, /aws.json, /graphql, /v1/env, /staging/.env, ...).
# Generic words (env/config/settings/...) match only outside our own API
# prefixes — see _is_scanner_path — so a player literally named "env"
# (/profile/chesscom/env) is never flagged.
BOT_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:"
    r"\.env\b|\.git\b|\.aws\b|\.vscode\b|\.DS_Store\b|"
    r"env\b|config\b|settings\b|aws\.json\b|graphql\b|"
    r"phpmyadmin\b|phpinfo\b|actuator\b|server-status\b|"
    r"wp-admin\b|wp-login\b|wp-content\b|wp-includes\b|wordpress\b|xmlrpc\.php\b"
    r")",
    re.IGNORECASE,
)

# Paths served by our own frontend/API: a probe word here (e.g. a username)
# must not mark the request as a bot.
KNOWN_API_PREFIXES = ("/profile/", "/games/", "/analyze/", "/graphs/", "/admin/")


def _is_scanner_path(path: str) -> bool:
    lowered = (path or "").lower()
    for prefix in KNOWN_API_PREFIXES:
        if lowered.startswith(prefix):
            return False
    return bool(BOT_PATH_PATTERN.search(path or ""))

# /profile/{site}/{username}, /games/page|last/..., /analyze/last/...,
# /graphs/performance/... — site and username are always the first two
# path segments after the route prefix.
SITE_USER_PATTERN = re.compile(
    r"^/(?:profile|games/(?:page|last)|analyze/last|graphs/performance)/([^/]+)/([^/?#]+)",
    re.IGNORECASE,
)


def detect_source(request: Request) -> str:
    """frontend when our own frontend made the call, bot for scripts, else other."""
    ua = request.headers.get("user-agent", "")
    if ua and BOT_UA_PATTERN.search(ua):
        return "bot"
    # Scanners rotate user-agents (same visitor seen as both bot and other),
    # so known probe paths and missing user-agents are bots too.
    path = request.scope.get("path", "")
    if not path:
        try:
            path = request.url.path
        except Exception:
            path = ""
    if _is_scanner_path(path):
        return "bot"
    if not ua.strip():
        return "bot"
    frontend_header = request.headers.get("x-frontend", "").strip().lower() in ("1", "true", "yes")
    origin = request.headers.get("origin", "").strip().rstrip("/")
    referer = request.headers.get("referer", "").strip()
    from_frontend_origin = False
    for allowed in CORS_ORIGINS:
        allowed = allowed.strip().rstrip("/")
        if not allowed:
            continue
        if origin == allowed or referer.startswith(allowed):
            from_frontend_origin = True
            break
    sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if frontend_header and (from_frontend_origin or sec_fetch_site in ("same-origin", "same-site")):
        return "frontend"
    return "other"


def parse_site_username(path: str) -> tuple[str | None, str | None]:
    match = SITE_USER_PATTERN.match(path or "")
    if not match:
        return None, None
    try:
        site = unquote(match.group(1)).strip().lower() or None
        username = unquote(match.group(2)).strip().lower() or None
    except Exception:
        return None, None
    if site and len(site) > 64:
        site = site[:64]
    if username and len(username) > 128:
        username = username[:128]
    return site, username


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # The rightmost entry is appended by our own nginx proxy and is the
        # only trusted one; anything to its left is client-controlled.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else None


def hash_ip(ip: str, salt: str) -> str:
    # Salted with the UTC date so the hash counts unique visitors per day
    # without allowing anyone (us included) to track a visitor across days.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{ip}:{day}:{salt}".encode()).hexdigest()


class StatsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.enabled = bool(DATABASE_URL)
        self._semaphore = asyncio.Semaphore(MAX_PENDING_EVENTS)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled or self._skipped(request):
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        error_detail = None
        if response.status_code >= 400:
            error_detail = await self._read_error_detail(response)
        self._record(self._build_event(request, response.status_code, duration_ms, error_detail))
        return response

    @staticmethod
    def _skipped(request: Request) -> bool:
        path = get_route_path(request.scope)
        return path in EXCLUDED_PATHS or path.startswith(EXCLUDED_PREFIXES)

    @staticmethod
    def _endpoint(request: Request) -> str:
        route = request.scope.get("route")
        return route.path if route is not None else UNMATCHED_ENDPOINT

    @staticmethod
    async def _read_error_detail(response: Response) -> str | None:
        """Extract a short human-readable error from a >= 400 response body.

        Streaming bodies are consumed and replayed so the client still gets
        the full response. Returns None when the body is empty/unreadable.
        """
        try:
            body = b""
            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is not None:
                chunks = []
                async for chunk in body_iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
                body = b"".join(chunks)[:ERROR_BODY_READ_LIMIT]

                async def _replay(data: bytes = body):
                    yield data

                response.body_iterator = _replay()
            else:
                raw = getattr(response, "body", b"") or b""
                body = (raw if isinstance(raw, bytes) else str(raw).encode())[:ERROR_BODY_READ_LIMIT]
            text = body.decode("utf-8", errors="replace").strip()
            if not text:
                return None
            try:
                payload = json.loads(text)
                if isinstance(payload, dict) and payload.get("detail") is not None:
                    detail = payload["detail"]
                    text = (
                        "; ".join(str(item) for item in detail)
                        if isinstance(detail, list)
                        else str(detail)
                    )
            except (ValueError, AttributeError):
                pass
            return text[:ERROR_DETAIL_MAX_LEN] or None
        except Exception:
            logger.warning("Failed to read error response body", exc_info=True)
            return None

    @staticmethod
    def _build_event(
        request: Request, status: int, duration_ms: int, error_detail: str | None = None
    ) -> dict:
        path = request.url.path
        site, username = parse_site_username(path)
        event = {
            "path": path,
            "endpoint": StatsMiddleware._endpoint(request),
            "method": request.method,
            "status": status,
            "duration_ms": duration_ms,
            "source": detect_source(request),
            "site": site,
            "username": username,
        }
        if error_detail:
            event["error_detail"] = error_detail
        ip = client_ip(request)
        if ip:
            event["ip_hash"] = hash_ip(ip, STATS_IP_SALT)
        state = request.state
        cache_hit = getattr(state, "analysis_cache_hit", None)
        if cache_hit is not None:
            event["cache_hit"] = cache_hit
        moves_count = getattr(state, "analysis_moves_count", None)
        if moves_count is not None:
            event["moves_count"] = moves_count
        return event

    def _record(self, event: dict) -> None:
        asyncio.create_task(self._insert(event))

    async def _insert(self, event: dict) -> None:
        async with self._semaphore:
            try:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    session.add(RequestEvent(**event))
                    await session.commit()
            except Exception:
                logger.warning("Failed to record request event", exc_info=True)

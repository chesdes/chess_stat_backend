import aiohttp
import asyncio
import logging

_session: aiohttp.ClientSession | None = None
logger = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 2
RETRY_DELAYS = (0.25, 0.5)

class UpstreamError(Exception):
    pass

class UpstreamHTTPError(UpstreamError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Upstream request failed with status {status_code}")

class UpstreamUnavailableError(UpstreamError):
    pass

async def init_http_client(timeout: int = 10):
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "chess-stats-api/1.0"},
        )

async def close_http_client():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None

async def get_json(url: str, headers: dict | None = None, timeout: int = 10):
    await init_http_client(timeout=timeout)
    request_headers = headers or {}
    request_headers.setdefault("User-Agent", "chess-stats-api/1.0")

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with _session.get(url, headers=request_headers) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientResponseError as e:
            if e.status not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                raise UpstreamHTTPError(e.status) from e
            logger.warning("Retrying upstream request after HTTP %s: %s", e.status, url)
            await asyncio.sleep(RETRY_DELAYS[attempt])
        except asyncio.TimeoutError as e:
            if attempt == MAX_RETRIES:
                raise UpstreamUnavailableError from e
            logger.warning("Retrying upstream request after timeout: %s", url)
            await asyncio.sleep(RETRY_DELAYS[attempt])
        except aiohttp.ClientError as e:
            raise UpstreamUnavailableError from e

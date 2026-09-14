import hashlib
import os


def _get_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


MAX_GAMES = _get_int("MAX_GAMES", 300, 1, 1000)
MAX_PAGE_SIZE = _get_int("MAX_PAGE_SIZE", 50, 1, 100)
MAX_PAGE_OFFSET = max(MAX_GAMES - 10, 0)
MAX_PGN_LENGTH = _get_int("MAX_PGN_LENGTH", 200_000, 1_000, 1_000_000)
MAX_ANALYSIS_RESULTS = _get_int("MAX_ANALYSIS_RESULTS", 5_000, 1, 20_000)
ANALYSIS_CONCURRENCY = _get_int("ANALYSIS_CONCURRENCY", 10, 1, 100)

CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "https://chess-stat.ru,https://www.chess-stat.ru,http://localhost:3000",
    ).split(",")
    if origin.strip()
)

DATABASE_URL = os.getenv("DATABASE_URL", "")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_TOKEN_TTL_SECONDS = _get_int("ADMIN_TOKEN_TTL_SECONDS", 86_400, 300, 604_800)
ADMIN_SECRET = os.getenv("ADMIN_SECRET") or (
    hashlib.sha256(f"chess-stat-admin:{ADMIN_PASSWORD}".encode()).hexdigest()
    if ADMIN_PASSWORD
    else ""
)

STATS_IP_SALT = os.getenv("STATS_IP_SALT", "")
STATS_RETENTION_DAYS = _get_int("STATS_RETENTION_DAYS", 400, 7, 3650)

ADMIN_MAX_ATTEMPTS = _get_int("ADMIN_MAX_ATTEMPTS", 5, 1, 100)
ADMIN_BAN_SECONDS = _get_int("ADMIN_BAN_SECONDS", 1800, 60, 86400)

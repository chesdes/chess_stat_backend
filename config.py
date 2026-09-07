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

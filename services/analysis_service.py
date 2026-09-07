import asyncio
import json

from config import ANALYSIS_CONCURRENCY
from models import AnalyzePayload, AnalyzeAndPgnPayload
from parsers import get_parser
from utils import Analyzer, RedisClient


class GameNotFoundError(ValueError):
    pass


class AnalysisCapacityError(RuntimeError):
    pass


class AnalysisService:
    CACHE_TTL_SECONDS = 259200

    def __init__(self, analyzer: Analyzer | None = None):
        self.analyzer = analyzer or Analyzer()
        self.analysis_semaphore = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

    async def get_last_game_analysis(self, site: str, username: str, index: int):
        parser = self._get_parser(site)
        games = await parser.get_last_games(username=username, limit=index)
        game = self._last_game(games)
        redis = RedisClient.get_client()
        cache_key = self._cache_key(site, game.url)
        cache = await redis.get(cache_key)

        if cache:
            try:
                result = json.loads(cache)
                if isinstance(result, (dict, list)):
                    return {"game": game, "analyze": result}
            except json.JSONDecodeError:
                pass

        return {"game": game, "analyze": None}

    async def save_last_game_analysis(self, site: str, username: str, index: int, payload: AnalyzePayload):
        parser = self._get_parser(site)
        games = await parser.get_last_games(username=username, limit=index)
        game = self._last_game(games)
        result = await self._calculate(
            [item.model_dump() for item in payload.results],
            game.pgn,
        )

        redis = RedisClient.get_client()
        await redis.set(
            self._cache_key(site, game.url),
            json.dumps(result),
            ex=self.CACHE_TTL_SECONDS,
        )
        return {"game": game, "analyze": result}

    async def analyze_pgn(self, payload: AnalyzeAndPgnPayload):
        result = await self._calculate(
            [item.model_dump() for item in payload.results],
            payload.pgn,
        )
        return {"analyze": result}

    async def _calculate(self, results: list[dict], pgn: str):
        if self.analysis_semaphore.locked():
            raise AnalysisCapacityError("Analysis capacity is temporarily exhausted")

        async with self.analysis_semaphore:
            return await asyncio.to_thread(self.analyzer.calculate_info, results, pgn)

    @staticmethod
    def _get_parser(site: str):
        parser = get_parser(site=site)
        if parser is None:
            raise ValueError("Unsupported chess site")
        return parser

    @staticmethod
    def _last_game(games):
        if not games:
            raise GameNotFoundError("Game not found")
        return games[-1]

    @staticmethod
    def _cache_key(site: str, url: str | None):
        if not url:
            raise ValueError("Game URL is missing")
        return f"analyze:{site}:{url.rstrip('/').split('/')[-1]}"

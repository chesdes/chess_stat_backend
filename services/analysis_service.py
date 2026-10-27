import asyncio
import json
import re
import secrets
from datetime import datetime, timezone
from io import StringIO
import chess.pgn
from config import ANALYSIS_CONCURRENCY
from models import AnalyzePayload, AnalyzeAndPgnPayload, Game, Player, PlayerCard
from parsers import get_parser
from starlette.requests import Request
from utils import Analyzer, RedisClient, normalize_pgn

class GameNotFoundError(ValueError):
    pass

class AnalysisCapacityError(RuntimeError):
    pass

SHARED_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
SHARED_ID_LENGTH = 9
SHARED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,12}$")
_STORE_SHARED_ATTEMPTS = 5

class AnalysisService:
    CACHE_TTL_SECONDS = 259200

    def __init__(self, analyzer: Analyzer | None = None):
        self.analyzer = analyzer or Analyzer()
        self.analysis_semaphore = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

    async def get_last_game_analysis(
        self,
        site: str,
        username: str,
        index: int,
        request: Request | None = None,
    ):
        parser = self._get_parser(site)
        games = await parser.get_last_games(username=username, limit=index)
        game = self._last_game(games)
        profiles_task = asyncio.create_task(self._load_profiles(game))
        redis = RedisClient.get_client()
        cache_key = self._cache_key(site, game.url)
        cache = await redis.get(cache_key)

        if cache:
            try:
                result = json.loads(cache)
                if isinstance(result, (dict, list)):
                    share_id = await self._latest_share_id(redis, site, game.url)
                    profiles = await profiles_task
                    if share_id is None:
                        try:
                            share_id = await self._store_shared_snapshot(redis, game, result, profiles)
                        except Exception:
                            share_id = None
                    moves = len(result) if isinstance(result, list) else None
                    self._set_stats_state(request, cache_hit=True, moves_count=moves)
                    return {"game": game, "profiles": profiles, "analyze": result, "share_id": share_id}
            except json.JSONDecodeError:
                pass

        self._set_stats_state(request, cache_hit=False)
        profiles = await profiles_task
        return {"game": game, "profiles": profiles, "analyze": None, "share_id": None}

    async def save_last_game_analysis(
        self,
        site: str,
        username: str,
        index: int,
        payload: AnalyzePayload,
        request: Request | None = None,
    ):
        parser = self._get_parser(site)
        games = await parser.get_last_games(username=username, limit=index)
        game = self._last_game(games)
        result, profiles = await asyncio.gather(
            self._calculate(
                [item.model_dump() for item in payload.results],
                game.pgn,
            ),
            self._load_profiles(game),
        )
        self._set_stats_state(request, cache_hit=False, moves_count=len(result))

        redis = RedisClient.get_client()
        await redis.set(
            self._cache_key(site, game.url),
            json.dumps(result),
            ex=self.CACHE_TTL_SECONDS,
        )
        share_id = await self._store_shared_snapshot(redis, game, result, profiles)
        return {"game": game, "profiles": profiles, "analyze": result, "share_id": share_id}

    async def analyze_pgn(
        self,
        payload: AnalyzeAndPgnPayload,
        request: Request | None = None,
    ):
        result = await self._calculate(
            [item.model_dump() for item in payload.results],
            payload.pgn,
        )
        self._set_stats_state(request, cache_hit=False, moves_count=len(result))
        game = self._game_from_pgn(payload.pgn)
        redis = RedisClient.get_client()
        profiles = self._empty_profiles()
        share_id = await self._store_shared_snapshot(redis, game, result, profiles)
        return {"game": game, "profiles": profiles, "analyze": result, "share_id": share_id}

    async def get_shared_analysis(
        self,
        share_id: str,
        request: Request | None = None,
    ):
        if not share_id or not SHARED_ID_RE.match(share_id):
            raise ValueError("Invalid share link")
        redis = RedisClient.get_client()
        raw = await redis.get(self._shared_key(share_id))
        if not raw:
            raise GameNotFoundError("Shared analysis not found or expired")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GameNotFoundError("Shared analysis not found or expired") from exc
        if not isinstance(payload, dict) or "game" not in payload or "analyze" not in payload:
            raise GameNotFoundError("Shared analysis not found or expired")
        try:
            game = Game.model_validate(payload["game"])
        except Exception as exc:
            raise GameNotFoundError("Shared analysis not found or expired") from exc
        result = payload["analyze"]
        if not isinstance(result, list):
            raise GameNotFoundError("Shared analysis not found or expired")
        profiles = self._profiles_from_snapshot(payload.get("profiles"))
        self._set_stats_state(request, cache_hit=True, moves_count=len(result))
        return {"game": game, "profiles": profiles, "analyze": result, "share_id": share_id}

    async def _latest_share_id(self, redis, site: str, url: str | None) -> str | None:
        by_game_key = self._shared_by_game_key(site, url)
        if by_game_key is None:
            return None
        raw = await redis.get(by_game_key)
        if not raw:
            return None
        share_id = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not SHARED_ID_RE.match(share_id):
            return None
        snapshot = await redis.get(self._shared_key(share_id))
        if not snapshot:
            return None
        return share_id

    async def _store_shared_snapshot(self, redis, game: Game, result, profiles: dict | None = None) -> str:
        payload = json.dumps(
            {
                "game": game.model_dump(mode="json"),
                "analyze": result,
                "site": game.site,
                "profiles": self._profiles_dump(profiles),
            }
        )
        for _ in range(_STORE_SHARED_ATTEMPTS):
            share_id = "".join(secrets.choice(SHARED_ID_ALPHABET) for _ in range(SHARED_ID_LENGTH))
            stored = await redis.set(
                self._shared_key(share_id),
                payload,
                ex=self.CACHE_TTL_SECONDS,
                nx=True,
            )
            if not stored:
                continue
            by_game_key = self._shared_by_game_key(game.site, game.url)
            if by_game_key is not None:
                await redis.set(by_game_key, share_id, ex=self.CACHE_TTL_SECONDS)
            return share_id
        raise RuntimeError("Unable to mint a share link, please retry")

    async def _load_profiles(self, game: Game) -> dict:
        if game.site == "pgn":
            return self._empty_profiles()
        try:
            white = (game.white.username or "").strip()
            black = (game.black.username or "").strip()
            if white and black and white.lower() == black.lower():
                card = await self._fetch_card(game.site, white)
                return {"white": card, "black": card}
            white_card, black_card = await asyncio.gather(
                self._fetch_card(game.site, white),
                self._fetch_card(game.site, black),
            )
            return {"white": white_card, "black": black_card}
        except Exception:
            return self._empty_profiles()

    @staticmethod
    async def _fetch_card(site: str, username: str) -> PlayerCard | None:
        if not username:
            return None
        try:
            parser = get_parser(site=site)
            if parser is None:
                return None
            card = await parser.get_profile_card(username=username)
            if card is None:
                return None
            return card if isinstance(card, PlayerCard) else PlayerCard.model_validate(card)
        except Exception:
            return None

    @staticmethod
    def _empty_profiles() -> dict:
        return {"white": None, "black": None}

    @staticmethod
    def _profiles_dump(profiles: dict | None) -> dict:
        if not isinstance(profiles, dict):
            return {"white": None, "black": None}
        out = {}
        for side in ("white", "black"):
            card = profiles.get(side)
            if card is None:
                out[side] = None
            elif isinstance(card, PlayerCard):
                out[side] = card.model_dump(mode="json")
            elif isinstance(card, dict):
                try:
                    out[side] = PlayerCard.model_validate(card).model_dump(mode="json")
                except Exception:
                    out[side] = None
            else:
                out[side] = None
        return out

    @staticmethod
    def _profiles_from_snapshot(raw) -> dict:
        if not isinstance(raw, dict):
            return {"white": None, "black": None}
        out = {}
        for side in ("white", "black"):
            card = raw.get(side)
            if card is None:
                out[side] = None
                continue
            try:
                out[side] = PlayerCard.model_validate(card)
            except Exception:
                out[side] = None
        return out

    @staticmethod
    def _game_from_pgn(pgn_text: str) -> Game:
        pgn_text = normalize_pgn(pgn_text)
        game_node = chess.pgn.read_game(StringIO(pgn_text))
        if game_node is None:
            raise ValueError("Failed to parse PGN text.")
        headers = game_node.headers

        def elo(raw) -> int:
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError, AttributeError):
                return 1500
            return value if value > 0 else 1500

        result = str(headers.get("Result", "*")).strip()
        if result == "1-0":
            white_result, black_result = "win", "loss"
        elif result == "0-1":
            white_result, black_result = "loss", "win"
        else:
            white_result, black_result = "draw", "draw"

        return Game(
            site="pgn",
            white=Player(
                username=str(headers.get("White", "White") or "White"),
                rating=elo(headers.get("WhiteElo", 1500)),
                result=white_result,
            ),
            black=Player(
                username=str(headers.get("Black", "Black") or "Black"),
                rating=elo(headers.get("BlackElo", 1500)),
                result=black_result,
            ),
            end_time=AnalysisService._end_time_from_headers(headers),
            pgn=pgn_text,
            time_control=str(headers.get("TimeControl", "") or ""),
            url=None,
        )

    @staticmethod
    def _end_time_from_headers(headers) -> datetime:
        date = str(headers.get("UTCDate", "") or headers.get("Date", "") or "")
        time = str(headers.get("UTCTime", "") or headers.get("Time", "") or "")
        match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", date)
        if match:
            clock = re.match(r"(\d{2}):(\d{2}):(\d{2})", time)
            hour, minute, second = (int(clock.group(1)), int(clock.group(2)), int(clock.group(3))) if clock else (0, 0, 0)
            try:
                return datetime(
                    int(match.group(1)), int(match.group(2)), int(match.group(3)),
                    hour, minute, second, tzinfo=timezone.utc,
                )
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @staticmethod
    def _set_stats_state(request: Request | None, cache_hit: bool, moves_count: int | None = None) -> None:
        if request is None:
            return
        request.state.analysis_cache_hit = cache_hit
        if moves_count is not None:
            request.state.analysis_moves_count = moves_count

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

    @staticmethod
    def _shared_key(share_id: str):
        return f"analyze:shared:{share_id}"

    @staticmethod
    def _shared_by_game_key(site: str, url: str | None):
        if not url:
            return None
        return f"analyze:shared:by-game:{site}:{url.rstrip('/').split('/')[-1]}"

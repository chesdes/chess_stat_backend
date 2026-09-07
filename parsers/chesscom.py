from datetime import datetime, timezone, timedelta
from models import PlayerProfile, Game, Player, Stats, TimeControl
from utils import get_json
from .base_parser import BaseParser
import asyncio

BASE_URL = "https://api.chess.com/pub/player"

class ChessComParser(BaseParser):
    site = "chesscom"

    async def get_profile(self, username: str) -> PlayerProfile | None:
        profile_url = f"{BASE_URL}/{username}"
        data = await get_json(profile_url)
        if not data:
            raise ValueError("profile data is null")

        country_data = await get_json(data.get("country"))
        country = country_data.get("code") if country_data else None

        stats_data = await self._get_stats(username)
        controls_keys = ["chess_rapid", "chess_blitz", "chess_bullet", "chess_daily"]

        controls = [self._make_tc(stats_data.get(key, {})) for key in controls_keys]

        stats = Stats(
            total_games=sum(i.games for i in controls),
            wins=sum(i.wins for i in controls),
            losses=sum(i.losses for i in controls),
            draws=sum(i.draws for i in controls),
            rapid=controls[0],
            blitz=controls[1],
            bullet=controls[2],
            daily=controls[3],
        )

        return PlayerProfile(
            site=self.site,
            username=data.get("username"),
            avatar=data.get("avatar"),
            title=data.get("title"),
            country=country,
            stats=stats,
        )

    def _make_tc(self, stat: dict) -> TimeControl:
        if not stat:
            return TimeControl(rating=0, games=0, wins=0, losses=0, draws=0)

        record = stat.get("record", {})
        last = stat.get("last", {})

        return TimeControl(
            rating=last.get("rating", 0),
            games=sum(record.get(x, 0) for x in ("win", "loss", "draw")),
            wins=record.get("win", 0),
            losses=record.get("loss", 0),
            draws=record.get("draw", 0),
        )

    async def _get_stats(self, username: str) -> dict:
        return await get_json(f"{BASE_URL}/{username}/stats") or {}

    async def get_last_games(self, username: str, limit: int = 100, offset: int = 0, control: str | None = None) -> list[Game]:
        archives = await get_json(f"{BASE_URL}/{username}/games/archives")
        if not archives:
            raise ValueError("archives empty")

        urls = reversed(archives.get("archives", []))
        all_games = []
        skipped_games = 0

        for url in urls:
            data = await get_json(url)
            if not data:
                continue

            for g in reversed(data.get("games", [])):
                if control and g.get("time_class") != control:
                    continue

                if skipped_games < offset:
                    skipped_games += 1
                    continue

                all_games.append(self._map_game(g))
                if len(all_games) >= limit:
                    return all_games

        return all_games

    async def get_month_games(self, username: str, year: int, month: int, control: str | None = None) -> list[Game]:
        url = f"{BASE_URL}/{username}/games/{year}/{month:02d}"
        data = await get_json(url)
        if not data:
            raise ValueError("data is null")

        games = [
            self._map_game(g)
            for g in data.get("games", [])
            if control is None or g.get("time_class") == control
        ]
        return games

    async def get_day_games(self, username: str, year: int, month: int, day: int, control: str | None = None):
        url = f"{BASE_URL}/{username}/games/{year}/{month:02d}"
        data = await get_json(url)
        if not data:
            raise ValueError("data is null")

        try:
            dt_l = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
            dt_h = int(datetime(year, month, day + 1, tzinfo=timezone.utc).timestamp())
        except Exception:
            raise ValueError("date is incorrect")

        games = [
            self._map_game(g)
            for g in reversed(data.get("games", []))
            if dt_l <= g.get("end_time", 0) < dt_h
            and (control is None or g.get("time_class") == control)
        ]
        return games

    async def get_games_in_period(self, username: str, start: int, end: int, control: str | None = None):
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end, tz=timezone.utc)

        current = start_dt
        games = []

        tasks = []
        while current <= end_dt+timedelta(days=1):
            url = f"{BASE_URL}/{username}/games/{current.year}/{current.month:02d}"
            tasks.append(get_json(url))
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                current = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        results = await asyncio.gather(*tasks)

        for data in results:
            if not data:
                continue
            for g in reversed(data.get("games", [])):
                if start <= g.get("end_time", 0) < end and (control is None or g.get("time_class") == control):
                    games.append(self._map_game(g))

        if not games:
            raise ValueError("games array is null")

        return games

    def _map_game(self, g: dict) -> Game:
        def parse_result(result: str) -> str:
            if result == "win":
                return "win"
            if result in {"loss", "resigned", "checkmated", "timeout", "abandoned"}:
                return "loss"
            return "draw"

        return Game(
            site=self.site,
            white=Player(
                username=g["white"].get("username"),
                rating=g["white"].get("rating", 0),
                result=parse_result(g["white"].get("result", "")),
            ),
            black=Player(
                username=g["black"].get("username"),
                rating=g["black"].get("rating", 0),
                result=parse_result(g["black"].get("result", "")),
            ),
            end_time=datetime.fromtimestamp(g.get("end_time", 0), tz=timezone.utc),
            time_control=g.get("time_class"),
            pgn=g.get("pgn"),
            url=g.get("url"),
        )

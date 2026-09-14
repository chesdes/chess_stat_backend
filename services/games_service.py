from config import MAX_GAMES
import math
from models import GamesPage
from parsers import get_parser


class GamesService:
    async def get_last_games(self, site: str, username: str, amount: int):
        parser = self._get_parser(site)
        return await parser.get_last_games(username=username, limit=amount)

    async def get_games_page(self, site: str, username: str, limit: int, offset: int):
        parser = self._get_parser(site)
        games = await parser.get_last_games(
            username=username,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(games) > limit and offset + limit < MAX_GAMES
        if has_more:
            total_pages = None
        else:
            # Last page reached: the true total is offset + games on it.
            total_pages = max(1, math.ceil((offset + len(games[:limit])) / limit))
        return GamesPage(
            games=games[:limit],
            offset=offset,
            limit=limit,
            total_pages=total_pages,
            has_more=has_more,
        )

    @staticmethod
    def _get_parser(site: str):
        parser = get_parser(site=site)
        if parser is None:
            raise ValueError("Unsupported chess site")
        return parser

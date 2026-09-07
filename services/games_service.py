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
        return GamesPage(
            games=games[:limit],
            offset=offset,
            limit=limit,
            total_pages=math.ceil(MAX_GAMES / limit),
            has_more=len(games) > limit and offset + limit < MAX_GAMES,
        )

    @staticmethod
    def _get_parser(site: str):
        parser = get_parser(site=site)
        if parser is None:
            raise ValueError("Unsupported chess site")
        return parser

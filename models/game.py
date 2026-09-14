from pydantic import BaseModel
from datetime import datetime
from typing import Literal

class Player(BaseModel):
    username: str
    rating: int
    result: Literal["win", "loss", "draw"]

class Game(BaseModel):
    site: str
    white: Player
    black: Player
    end_time: datetime
    pgn: str
    time_control: str | None = None
    url: str | None = None

class GamesPage(BaseModel):
    games: list[Game]
    offset: int
    limit: int
    # Exact page count is known only once the last page is reached
    # (has_more=False); otherwise None — the backend never scans the full
    # archive history just to count games.
    total_pages: int | None
    has_more: bool

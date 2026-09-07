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
    total_pages: int
    has_more: bool

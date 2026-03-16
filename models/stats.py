from pydantic import BaseModel

class TimeControl(BaseModel):
    rating: int
    games: int
    wins: int
    losses: int
    draws: int

class Stats(BaseModel):
    total_games: int
    wins: int
    losses: int
    draws: int
    rapid: TimeControl
    blitz: TimeControl
    bullet: TimeControl
    daily: TimeControl
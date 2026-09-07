from pydantic import BaseModel


class PerformancePoint(BaseModel):
    timestamp: int
    value: int
    rating: int
    games: int


class PerformanceGraph(BaseModel):
    max: int
    min: int
    full: int
    points: list[PerformancePoint]

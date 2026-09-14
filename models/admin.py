from datetime import datetime

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AdminLoginResponse(BaseModel):
    token: str
    expires_in: int


class StatsSummary(BaseModel):
    requests: int
    unique_visitors: int
    errors: int
    avg_duration_ms: int | None
    frontend: int = 0
    other: int = 0
    period: str = "30d"


class DailyStatsPoint(BaseModel):
    date: str
    requests: int
    analyses: int
    unique_visitors: int
    errors: int
    bots: int = 0


class EndpointStats(BaseModel):
    endpoint: str
    requests: int
    errors: int
    avg_duration_ms: int | None
    unique_visitors: int = 0
    frontend: int = 0
    other: int = 0


class RecentRequest(BaseModel):
    ts: datetime
    method: str
    path: str
    endpoint: str
    status: int
    duration_ms: int
    source: str = "other"
    site: str | None = None
    username: str | None = None
    cache_hit: bool | None = None
    moves_count: int | None = None
    visitor_name: str | None = None
    ip_hash: str | None = None
    error_detail: str | None = None


class AnalysisStats(BaseModel):
    total: int
    cache_hit_rate: float
    avg_moves: float | None
    local: int = 0
    cache: int = 0
    local_moves: int = 0
    cache_moves: int = 0
    avg_local_moves: float | None = None


class TopEntry(BaseModel):
    name: str
    requests: int
    unique_visitors: int


class TopStats(BaseModel):
    sites: list[TopEntry] = []
    usernames: list[TopEntry] = []


class AdminStatsResponse(BaseModel):
    summary: StatsSummary
    daily: list[DailyStatsPoint]
    endpoints: list[EndpointStats]
    recent: list[RecentRequest]
    analysis: AnalysisStats
    top: TopStats = TopStats()
    period: str = "30d"

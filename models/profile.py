from pydantic import BaseModel
from .stats import Stats

class PlayerProfile(BaseModel):
    site: str
    username: str
    avatar: str | None = None
    country: str | None = None
    title: str | None = None
    stats: Stats
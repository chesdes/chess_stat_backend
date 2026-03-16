from abc import ABC, abstractmethod
from models import PlayerProfile, Game

class BaseParser(ABC):
    site: str

    @abstractmethod
    async def get_profile(self, username: str) -> PlayerProfile | None:
        pass

    @abstractmethod
    async def get_last_games(self, username: str, limit: int = 100, control: str | None = None) -> list[Game]:
        pass

    @abstractmethod
    async def get_month_games(self, username: str, year: int, month: int, control: str | None = None) -> list[Game]:
        pass

    @abstractmethod
    async def get_day_games(self, username: str, year: int, month: int, day: int, control: str | None = None) -> list[Game]:
        pass

    @abstractmethod
    async def get_games_in_period(self, username: str, start: int, end: int, control: str | None = None) -> list[Game]:
        pass

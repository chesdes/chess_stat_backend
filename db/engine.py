from config import DATABASE_URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_session_factory() -> async_sessionmaker:
    global _engine, _session_factory
    if _session_factory is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None

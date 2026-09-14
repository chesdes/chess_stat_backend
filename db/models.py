from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .engine import Base


class RequestEvent(Base):
    """One row per API request, recorded by the stats middleware."""

    __tablename__ = "request_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256(ip + salt + UTC date): enough to count unique visitors per day,
    # impossible to recover the IP or track a visitor across days.
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    moves_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "frontend" when the request came from our own frontend
    # (X-Frontend header + Origin check), "bot" for crawlers/scripts,
    # otherwise "other".
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    # Parsed from path for routes like /profile/{site}/{username};
    # NULL when the path carries no site/username.
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Truncated error response body ({"detail": ...} or raw text) for
    # status >= 400; NULL for successful requests and old rows.
    error_detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    __table_args__ = (
        Index("ix_request_events_ts", "ts"),
        Index("ix_request_events_endpoint_ts", "endpoint", "ts"),
        Index("ix_request_events_ts_source", "ts", "source"),
        Index("ix_request_events_ts_site", "ts", "site"),
        Index("ix_request_events_ip_ts", "ip_hash", "ts"),
    )

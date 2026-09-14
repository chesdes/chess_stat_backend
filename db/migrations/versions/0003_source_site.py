"""Add source/site/username columns to request_events.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Idempotent: the prod DB already had these columns (added out-of-band)
    # while alembic was still at 0002, so only add what is actually missing.
    cols = _existing_columns("request_events")
    if "source" not in cols:
        op.add_column("request_events", sa.Column("source", sa.String(length=16), nullable=True))
    if "site" not in cols:
        op.add_column("request_events", sa.Column("site", sa.String(length=64), nullable=True))
    if "username" not in cols:
        op.add_column("request_events", sa.Column("username", sa.String(length=128), nullable=True))
    op.execute(sa.text("UPDATE request_events SET source = 'other' WHERE source IS NULL"))
    # Old rows predate the column: leave site/username NULL, new rows are
    # parsed in middleware (SQLite has no REGEXP for a SQL backfill).
    op.alter_column("request_events", "source", existing_type=sa.String(length=16), nullable=False)
    indexes = _existing_indexes("request_events")
    if "ix_request_events_ts_source" not in indexes:
        op.create_index("ix_request_events_ts_source", "request_events", ["ts", "source"])
    if "ix_request_events_ts_site" not in indexes:
        op.create_index("ix_request_events_ts_site", "request_events", ["ts", "site"])
    if "ix_request_events_ip_ts" not in indexes:
        op.create_index("ix_request_events_ip_ts", "request_events", ["ip_hash", "ts"])


def downgrade() -> None:
    indexes = _existing_indexes("request_events")
    for name in (
        "ix_request_events_ip_ts",
        "ix_request_events_ts_site",
        "ix_request_events_ts_source",
    ):
        if name in indexes:
            op.drop_index(name, table_name="request_events")
    cols = _existing_columns("request_events")
    for name in ("username", "site", "source"):
        if name in cols:
            op.drop_column("request_events", name)

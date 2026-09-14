"""Initial request_events table

Revision ID: 0001
Revises:
Create Date: 2026-09-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=True),
        sa.Column("moves_count", sa.Integer(), nullable=True),
    )
    op.create_index("ix_request_events_ts", "request_events", ["ts"])
    op.create_index("ix_request_events_endpoint_ts", "request_events", ["endpoint", "ts"])


def downgrade() -> None:
    op.drop_index("ix_request_events_endpoint_ts", table_name="request_events")
    op.drop_index("ix_request_events_ts", table_name="request_events")
    op.drop_table("request_events")

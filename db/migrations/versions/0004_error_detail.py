"""Store truncated error response bodies for failed requests.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("request_events")}
    if "error_detail" not in cols:
        op.add_column(
            "request_events",
            sa.Column("error_detail", sa.String(length=1000), nullable=True),
        )


def downgrade() -> None:
    cols = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("request_events")}
    if "error_detail" in cols:
        op.drop_column("request_events", "error_detail")

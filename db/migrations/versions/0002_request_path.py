"""Store the request path for admin request history.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("request_events", sa.Column("path", sa.String(length=2048), nullable=True))
    op.execute(sa.text("UPDATE request_events SET path = endpoint WHERE path IS NULL"))
    op.alter_column("request_events", "path", existing_type=sa.String(length=2048), nullable=False)


def downgrade() -> None:
    op.drop_column("request_events", "path")
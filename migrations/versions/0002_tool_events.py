"""Record investigation tool events for the live UI.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation_runs", Column("events", JSONB, nullable=False, server_default="[]")
    )


def downgrade() -> None:
    op.drop_column("investigation_runs", "events")

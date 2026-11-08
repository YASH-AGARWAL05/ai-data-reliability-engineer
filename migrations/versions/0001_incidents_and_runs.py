"""Create incidents and investigation runs.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("title", String(200), nullable=False),
        Column("scenario", String(64), nullable=False),
        Column("seed", Integer, nullable=False),
        Column("project_path", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_table(
        "investigation_runs",
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column(
            "incident_id",
            Uuid(as_uuid=True),
            ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("status", String(32), nullable=False),
        Column("cancel_requested", Boolean, nullable=False),
        Column("lease_token", Uuid(as_uuid=True)),
        Column("lease_until", DateTime(timezone=True)),
        Column("worker_failures", Integer, nullable=False),
        Column("max_steps", Integer, nullable=False),
        Column("max_tokens", Integer, nullable=False),
        Column("steps", Integer, nullable=False),
        Column("tokens", Integer, nullable=False),
        Column("evidence", JSONB, nullable=False),
        Column("hypotheses", JSONB, nullable=False),
        Column("diff", Text, nullable=False),
        Column("comparison", JSONB),
        Column("reasons", JSONB, nullable=False),
        Column("review_status", String(16)),
        Column("review_reason", Text),
        Column("reviewed_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint(
            "status in ('queued','running','cancelling','cancelled',"
            "'ready_for_review','failed','exhausted')",
            name="ck_investigation_status",
        ),
        CheckConstraint(
            "review_status is null or review_status in ('approved','rejected')",
            name="ck_review_status",
        ),
    )
    op.create_index("ix_runs_claim", "investigation_runs", ["status", "lease_until", "created_at"])
    op.create_index("ix_runs_incident", "investigation_runs", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_incident", table_name="investigation_runs")
    op.drop_index("ix_runs_claim", table_name="investigation_runs")
    op.drop_table("investigation_runs")
    op.drop_table("incidents")

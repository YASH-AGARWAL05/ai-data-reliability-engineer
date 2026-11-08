"""Relational state for incidents, worker claims, and review decisions."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    project_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        Index("ix_runs_claim", "status", "lease_until", "created_at"),
        Index("ix_runs_incident", "incident_id"),
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

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    events: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    hypotheses: Mapped[dict[str, dict[str, object]]] = mapped_column(JSONB, default=dict)
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comparison: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    review_status: Mapped[str | None] = mapped_column(String(16))
    review_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

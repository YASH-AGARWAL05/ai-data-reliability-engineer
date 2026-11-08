"""Local incident and investigation HTTP API."""

import csv
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from ai_data_reliability.analytics.scenarios import TEMPLATE, generate
from ai_data_reliability.evaluation.report import EvaluationReport, load_reports
from ai_data_reliability.service.config import Settings
from ai_data_reliability.service.db import Incident, InvestigationRun

Scenario = Literal[
    "clean",
    "duplicate_payments",
    "join_cardinality",
    "late_refunds",
    "missing_currency_conversion",
    "schema_drift",
]


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    scenario: Scenario
    seed: int = Field(ge=0, le=2_147_483_647)


class IncidentView(BaseModel):
    id: uuid.UUID
    title: str
    scenario: str
    seed: int
    created_at: datetime


class RunCreate(BaseModel):
    max_steps: int = Field(default=10, ge=1, le=50)
    max_tokens: int = Field(default=1000, ge=1, le=100_000)


class RunView(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    status: str
    cancel_requested: bool
    steps: int
    tokens: int
    max_steps: int
    max_tokens: int
    evidence: list[dict[str, object]]
    events: list[dict[str, object]]
    hypotheses: dict[str, dict[str, object]]
    diff: str
    comparison: dict[str, object] | None
    reasons: list[str]
    review_status: str | None
    review_reason: str | None
    created_at: datetime
    updated_at: datetime


class ReviewRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def _incident_view(row: Incident) -> IncidentView:
    return IncidentView(
        id=row.id,
        title=row.title,
        scenario=row.scenario,
        seed=row.seed,
        created_at=row.created_at,
    )


def _run_view(row: InvestigationRun) -> RunView:
    return RunView(
        id=row.id,
        incident_id=row.incident_id,
        status=row.status,
        cancel_requested=row.cancel_requested,
        steps=row.steps,
        tokens=row.tokens,
        max_steps=row.max_steps,
        max_tokens=row.max_tokens,
        evidence=row.evidence,
        events=row.events,
        hypotheses=row.hypotheses,
        diff=row.diff,
        comparison=row.comparison,
        reasons=row.reasons,
        review_status=row.review_status,
        review_reason=row.review_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run_or_404(session: Session, run_id: uuid.UUID, *, lock: bool = False) -> InvestigationRun:
    statement = select(InvestigationRun).where(InvestigationRun.id == run_id)
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found")
    return row


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or create_engine(settings.database_url, pool_pre_ping=True)
    app = FastAPI(title="AI Data Reliability Engineer", version="0.6.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        return {"status": "ok"}

    @app.get("/catalog")
    def catalog() -> dict[str, object]:
        datasets: list[dict[str, object]] = []
        for path in sorted((TEMPLATE / "seeds").glob("*.csv")):
            with path.open(newline="", encoding="utf-8") as stream:
                datasets.append({"name": path.stem, "rows": sum(1 for _ in csv.DictReader(stream))})
        models = [path.stem for path in sorted((TEMPLATE / "models").rglob("*.sql"))]
        tests = len(list((TEMPLATE / "tests").glob("*.sql")))
        return {"pipeline": "jaffle_shop", "datasets": datasets, "models": models, "tests": tests}

    @app.get("/evaluations", response_model=list[EvaluationReport])
    def list_evaluations() -> list[EvaluationReport]:
        return load_reports(settings.run_root.parent / "evaluations")

    @app.post("/incidents", response_model=IncidentView, status_code=status.HTTP_201_CREATED)
    def create_incident(request: IncidentCreate) -> IncidentView:
        settings.run_root.mkdir(parents=True, exist_ok=True)
        incident_id = uuid.uuid4()
        generated = generate(request.scenario, request.seed, settings.run_root / str(incident_id))
        with Session(engine) as session, session.begin():
            row = Incident(
                id=incident_id,
                title=request.title,
                scenario=request.scenario,
                seed=request.seed,
                project_path=str(generated.project),
            )
            session.add(row)
            session.flush()
            return _incident_view(row)

    @app.get("/incidents", response_model=list[IncidentView])
    def list_incidents() -> list[IncidentView]:
        with Session(engine) as session:
            rows = session.scalars(select(Incident).order_by(Incident.created_at.desc()).limit(100))
            return [_incident_view(row) for row in rows]

    @app.get("/incidents/{incident_id}", response_model=IncidentView)
    def get_incident(incident_id: uuid.UUID) -> IncidentView:
        with Session(engine) as session:
            row = session.get(Incident, incident_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
            return _incident_view(row)

    @app.post(
        "/incidents/{incident_id}/investigations",
        response_model=RunView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_run(incident_id: uuid.UUID, request: RunCreate) -> RunView:
        with Session(engine) as session, session.begin():
            if session.get(Incident, incident_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
            row = InvestigationRun(
                id=uuid.uuid4(),
                incident_id=incident_id,
                status="queued",
                cancel_requested=False,
                worker_failures=0,
                max_steps=request.max_steps,
                max_tokens=request.max_tokens,
                steps=0,
                tokens=0,
                evidence=[],
                events=[],
                hypotheses={},
                diff="",
                comparison=None,
                reasons=[],
            )
            session.add(row)
            session.flush()
            return _run_view(row)

    @app.get("/investigations", response_model=list[RunView])
    def list_runs(incident_id: uuid.UUID | None = None) -> list[RunView]:
        statement = select(InvestigationRun).order_by(InvestigationRun.created_at.desc()).limit(100)
        if incident_id is not None:
            statement = statement.where(InvestigationRun.incident_id == incident_id)
        with Session(engine) as session:
            return [_run_view(row) for row in session.scalars(statement)]

    @app.get("/investigations/{run_id}", response_model=RunView)
    def get_run(run_id: uuid.UUID) -> RunView:
        with Session(engine) as session:
            return _run_view(_run_or_404(session, run_id))

    @app.post("/investigations/{run_id}/cancel", response_model=RunView)
    def cancel(run_id: uuid.UUID) -> RunView:
        with Session(engine) as session, session.begin():
            row = _run_or_404(session, run_id, lock=True)
            if row.status == "queued":
                row.status = "cancelled"
            elif row.status == "running":
                row.cancel_requested = True
                row.status = "cancelling"
            elif row.status != "cancelling":
                raise HTTPException(status.HTTP_409_CONFLICT, "Run cannot be cancelled")
            session.flush()
            return _run_view(row)

    @app.post("/investigations/{run_id}/resume", response_model=RunView)
    def resume(run_id: uuid.UUID) -> RunView:
        with Session(engine) as session, session.begin():
            row = _run_or_404(session, run_id, lock=True)
            if row.status != "cancelled":
                raise HTTPException(status.HTTP_409_CONFLICT, "Only cancelled runs can resume")
            row.status = "queued"
            row.cancel_requested = False
            row.lease_token = None
            row.lease_until = None
            session.flush()
            return _run_view(row)

    def review(run_id: uuid.UUID, request: ReviewRequest, decision: str) -> RunView:
        with Session(engine) as session, session.begin():
            row = _run_or_404(session, run_id, lock=True)
            if row.status != "ready_for_review" or row.review_status is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Run is not awaiting review")
            if (
                not row.comparison
                or not row.comparison.get("eligible_for_review")
                or not any(item.get("status") == "verified" for item in row.hypotheses.values())
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "Run has no verified repair")
            row.review_status = decision
            row.review_reason = request.reason
            row.reviewed_at = datetime.now(UTC)
            session.flush()
            return _run_view(row)

    @app.post("/investigations/{run_id}/approve", response_model=RunView)
    def approve(run_id: uuid.UUID, request: ReviewRequest) -> RunView:
        return review(run_id, request, "approved")

    @app.post("/investigations/{run_id}/reject", response_model=RunView)
    def reject(run_id: uuid.UUID, request: ReviewRequest) -> RunView:
        return review(run_id, request, "rejected")

    return app


app = create_app()

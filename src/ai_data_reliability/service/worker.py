"""One-step PostgreSQL queue worker for checkpointed investigations."""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, and_, create_engine, or_, select
from sqlalchemy.orm import Session

from ai_data_reliability.investigator.graph import InvestigationState, Investigator
from ai_data_reliability.investigator.provider import DecisionProvider, FakeProvider, OpenAIProvider
from ai_data_reliability.investigator.sandbox import InvestigatorTools
from ai_data_reliability.service.config import Settings
from ai_data_reliability.service.db import Incident, InvestigationRun

LOGGER = logging.getLogger(__name__)
LEASE = timedelta(minutes=10)


@dataclass(frozen=True)
class Claim:
    run_id: uuid.UUID
    token: uuid.UUID
    project: Path
    max_steps: int
    max_tokens: int
    recorded_steps: int
    cancel_requested: bool


class Worker:
    def __init__(
        self, settings: Settings, engine: Engine, provider: DecisionProvider | None = None
    ) -> None:
        settings.run_root.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.investigator = Investigator(
            InvestigatorTools(settings.run_root, settings.sandbox_root),
            provider or FakeProvider(),
            settings.checkpoint_path,
        )

    def _claim(self) -> Claim | None:
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            statement = (
                select(InvestigationRun)
                .where(
                    or_(
                        InvestigationRun.status == "queued",
                        and_(
                            InvestigationRun.status.in_(("running", "cancelling")),
                            InvestigationRun.lease_until < now,
                        ),
                    )
                )
                .order_by(InvestigationRun.created_at, InvestigationRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = session.scalar(statement)
            if row is None:
                return None
            incident = session.get(Incident, row.incident_id)
            if incident is None:
                raise RuntimeError("Investigation has no incident")
            token = uuid.uuid4()
            requested = row.cancel_requested
            row.status = "cancelling" if requested else "running"
            row.lease_token = token
            row.lease_until = now + LEASE
            return Claim(
                row.id,
                token,
                Path(incident.project_path),
                row.max_steps,
                row.max_tokens,
                row.steps,
                requested,
            )

    def _state_or_none(self, run_id: str) -> InvestigationState | None:
        try:
            return self.investigator.state(run_id)
        except KeyError:
            return None

    def _work(self, claim: Claim) -> InvestigationState | None:
        run_id = str(claim.run_id)
        state = self._state_or_none(run_id)
        if claim.cancel_requested:
            if state is not None:
                return self.investigator.cancel(run_id)
            return None
        if state is None:
            self.investigator.start(
                claim.project,
                max_steps=claim.max_steps,
                max_tokens=claim.max_tokens,
                run_id=run_id,
            )
            return self.investigator.state(run_id)
        if state["status"] == "cancelled":
            state = self.investigator.resume(run_id)
        if state["steps"] > claim.recorded_steps:
            return state
        return self.investigator.advance(run_id)

    def process_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        try:
            state = self._work(claim)
        except Exception:
            LOGGER.exception("Investigation step failed for %s", claim.run_id)
            with Session(self.engine) as session, session.begin():
                row = session.get(InvestigationRun, claim.run_id, with_for_update=True)
                if row is None or row.lease_token != claim.token:
                    return True
                row.worker_failures += 1
                row.status = "failed" if row.worker_failures >= 3 else "queued"
                row.reasons = ["Worker step failed"]
                row.lease_token = None
                row.lease_until = None
            return True
        with Session(self.engine) as session, session.begin():
            row = session.get(InvestigationRun, claim.run_id, with_for_update=True)
            if row is None or row.lease_token != claim.token:
                return True
            if row.cancel_requested and state is not None and state["status"] == "running":
                state = self.investigator.cancel(str(claim.run_id))
            if state is None:
                row.status = "cancelled"
            else:
                row.status = "queued" if state["status"] == "running" else state["status"]
                row.steps = state["steps"]
                row.tokens = state["tokens"]
                row.evidence = state["evidence"]
                row.events = state["events"]
                row.hypotheses = state["hypotheses"]
                row.diff = state["diff"]
                row.comparison = state["comparison"]
                row.reasons = state["reasons"]
            row.cancel_requested = False
            row.worker_failures = 0
            row.lease_token = None
            row.lease_until = None
        return True

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        while True:
            if not self.process_once():
                time.sleep(poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    selected = os.environ.get("AI_PROVIDER", "fake")
    if selected == "fake":
        provider: DecisionProvider = FakeProvider()
    elif selected == "openai":
        model = os.environ.get("OPENAI_MODEL")
        if not model:
            raise ValueError("OPENAI_MODEL is required when AI_PROVIDER=openai")
        provider = OpenAIProvider(model)
    else:
        raise ValueError("AI_PROVIDER must be fake or openai")
    Worker(settings, engine, provider).run_forever()


if __name__ == "__main__":
    main()

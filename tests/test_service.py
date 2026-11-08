import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session

from ai_data_reliability.investigator.graph import InvestigationState
from ai_data_reliability.service.api import create_app
from ai_data_reliability.service.config import Settings
from ai_data_reliability.service.db import Incident, InvestigationRun
from ai_data_reliability.service.worker import Claim, Worker

DATABASE_URL = "postgresql+psycopg://reliability@127.0.0.1:55432/reliability"


ServiceFixture = tuple[TestClient, Worker, Engine, list[uuid.UUID]]


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ServiceFixture]:
    settings = Settings(
        DATABASE_URL, tmp_path / "runs", tmp_path / "sandboxes", tmp_path / "checkpoints.sqlite"
    )
    engine = create_engine(DATABASE_URL)
    assert {"incidents", "investigation_runs"} <= set(inspect(engine).get_table_names())
    client = TestClient(create_app(settings, engine))
    worker = Worker(settings, engine)
    incident_ids: list[uuid.UUID] = []
    yield client, worker, engine, incident_ids
    with Session(engine) as session, session.begin():
        for incident_id in incident_ids:
            row = session.get(Incident, incident_id)
            if row is not None:
                session.delete(row)
    engine.dispose()


def _create(client: TestClient, incident_ids: list[uuid.UUID]) -> tuple[str, str]:
    incident = client.post(
        "/incidents",
        json={"title": "Revenue spike", "scenario": "duplicate_payments", "seed": 7},
    )
    assert incident.status_code == 201, incident.text
    assert "ground_truth" not in incident.text
    incident_id = incident.json()["id"]
    incident_ids.append(uuid.UUID(incident_id))
    run = client.post(f"/incidents/{incident_id}/investigations", json={})
    assert run.status_code == 201, run.text
    return incident_id, run.json()["id"]


def test_api_worker_review_flow(service: ServiceFixture) -> None:
    client, worker, engine, incident_ids = service
    incident_id, run_id = _create(client, incident_ids)
    with Session(engine) as session:
        incident = session.get(Incident, uuid.UUID(incident_id))
        assert incident is not None
        project = Path(incident.project_path)
    source_code = (project / "models/staging/stg_payments.sql").read_bytes()
    assert (
        client.post(f"/investigations/{run_id}/approve", json={"reason": "checked"}).status_code
        == 409
    )
    assert client.get("/incidents").json()[0]["id"] == incident_id
    assert client.get("/evaluations").json() == []
    catalog = client.get("/catalog").json()
    assert {item["name"] for item in catalog["datasets"]} == {
        "raw_customers",
        "raw_orders",
        "raw_payments",
    }
    assert "fct_daily_revenue" in catalog["models"]
    assert client.get(f"/incidents/{incident_id}").status_code == 200
    assert client.get(f"/investigations?incident_id={incident_id}").json()[0]["id"] == run_id
    for _ in range(5):
        assert worker.process_once()
        current = client.get(f"/investigations/{run_id}").json()
        if current["status"] != "queued":
            break
    assert current["status"] == "ready_for_review", current["reasons"]
    assert current["comparison"]["eligible_for_review"] is True
    assert current["hypotheses"]["duplicate_payment_ids"]["status"] == "verified"
    assert len(current["evidence"]) == 2
    assert {item["tool"] for item in current["events"]} >= {
        "run_sql",
        "run_dbt_tests",
        "compare_metrics",
    }
    assert current["diff"]
    assert (project / "models/staging/stg_payments.sql").read_bytes() == source_code
    reviewed = client.post(f"/investigations/{run_id}/approve", json={"reason": "evidence checked"})
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "approved"
    assert client.post(f"/investigations/{run_id}/reject", json={"reason": "no"}).status_code == 409
    assert client.post(f"/investigations/{run_id}/cancel").status_code == 409
    assert (project / "models/staging/stg_payments.sql").read_bytes() == source_code


def test_cancel_resume_and_checkpoint_recovery(service: ServiceFixture) -> None:
    client, worker, engine, incident_ids = service
    _, run_id = _create(client, incident_ids)
    assert client.post(f"/investigations/{run_id}/cancel").json()["status"] == "cancelled"
    assert not worker.process_once()
    assert client.post(f"/investigations/{run_id}/resume").json()["status"] == "queued"
    assert worker.process_once()
    assert worker.investigator.state(run_id)["steps"] == 1
    with Session(engine) as session, session.begin():
        row = session.get(InvestigationRun, uuid.UUID(run_id))
        assert row is not None
        row.steps = 0
        row.status = "running"
        row.lease_token = uuid.uuid4()
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert worker.process_once()
    assert client.get(f"/investigations/{run_id}").json()["steps"] == 1
    with Session(engine) as session, session.begin():
        row = session.get(InvestigationRun, uuid.UUID(run_id))
        assert row is not None
        row.status = "running"
        row.lease_token = uuid.uuid4()
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert client.post(f"/investigations/{run_id}/cancel").json()["status"] == "cancelling"
    assert worker.process_once()
    assert client.get(f"/investigations/{run_id}").json()["status"] == "cancelled"
    assert worker.investigator.state(run_id)["status"] == "cancelled"
    assert client.post(f"/investigations/{run_id}/resume").json()["status"] == "queued"
    assert worker.process_once()
    assert client.get(f"/investigations/{run_id}").json()["steps"] == 2


def test_validation_and_missing_resources(service: ServiceFixture) -> None:
    client, _, engine, incident_ids = service
    assert (
        client.post(
            "/incidents", json={"title": "Bad", "scenario": "../../outside", "seed": 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/incidents", json={"title": "Bad", "scenario": "clean", "seed": -1}
        ).status_code
        == 422
    )
    missing = uuid.uuid4()
    assert client.get(f"/incidents/{missing}").status_code == 404
    assert client.get(f"/investigations/{missing}").status_code == 404
    assert client.post(f"/incidents/{missing}/investigations", json={}).status_code == 404
    assert client.get("/health").json() == {"status": "ok"}
    _, run_id = _create(client, incident_ids)
    with Session(engine) as session, session.begin():
        row = session.get(InvestigationRun, uuid.UUID(run_id))
        assert row is not None
        row.status = "ready_for_review"
        row.comparison = {"eligible_for_review": True}
        row.hypotheses = {"duplicate_payment_ids": {"status": "verified"}}
    rejected = client.post(f"/investigations/{run_id}/reject", json={"reason": "Needs revision"})
    assert rejected.status_code == 200
    assert rejected.json()["review_status"] == "rejected"
    assert (
        client.post(f"/investigations/{run_id}/approve", json={"reason": "no"}).status_code == 409
    )


def test_worker_claim_is_exclusive_until_lease_expires(service: ServiceFixture) -> None:
    client, worker, engine, incident_ids = service
    _, run_id = _create(client, incident_ids)
    first = worker._claim()
    assert first is not None and str(first.run_id) == run_id
    assert (
        Worker(
            Settings(
                DATABASE_URL,
                worker.investigator.tools.source_root,
                worker.investigator.tools.sandbox_root,
                worker.investigator.checkpoint_path,
            ),
            engine,
        )._claim()
        is None
    )
    with Session(engine) as session, session.begin():
        row = session.get(InvestigationRun, uuid.UUID(run_id))
        assert row is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    second = worker._claim()
    assert second is not None and second.run_id == first.run_id
    assert second.token != first.token


def test_worker_failure_retries_are_bounded(
    service: ServiceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, worker, _, incident_ids = service
    _, run_id = _create(client, incident_ids)

    def fail_step(_claim: Claim) -> InvestigationState | None:
        raise RuntimeError("Injected worker failure")

    monkeypatch.setattr(worker, "_work", fail_step)
    for expected in ("queued", "queued", "failed"):
        assert worker.process_once()
        assert client.get(f"/investigations/{run_id}").json()["status"] == expected
    assert worker.process_once() is False

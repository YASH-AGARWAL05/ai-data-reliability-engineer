import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from openai import OpenAI

from ai_data_reliability.analytics.scenarios import TEMPLATE, generate
from ai_data_reliability.evaluation.contracts import CAUSES
from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import Investigator
from ai_data_reliability.investigator.provider import (
    Decision,
    FakeProvider,
    OllamaLocalProvider,
    OpenAIProvider,
)
from ai_data_reliability.investigator.sandbox import InvestigatorTools, SQLResult, ToolError


def _investigator(
    tmp_path: Path, *, tools_type: type[InvestigatorTools] = InvestigatorTools
) -> tuple[Investigator, Path]:
    source_root = tmp_path / "runs"
    source = generate("duplicate_payments", 7, source_root / "incident")
    tools = tools_type(source_root, tmp_path / "sandboxes")
    return Investigator(tools, FakeProvider(), tmp_path / "checkpoints.sqlite"), source.project


def test_duplicate_payment_investigation_end_to_end(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path)
    source_code = (project / "models/staging/stg_payments.sql").read_bytes()
    run_id = investigator.start(project)
    final = investigator.run(run_id)
    assert final["status"] == "ready_for_review", final["reasons"]
    assert final["hypotheses"]["duplicate_payment_ids"]["status"] == "verified"
    assert final["hypotheses"]["currency_join_cardinality"]["status"] == "refuted"
    assert final["hypotheses"]["duplicate_payment_ids"]["competing_evidence_id"] == 1
    assert len(final["evidence"]) == 2
    assert final["diff"].startswith("--- a/models/staging/stg_payments.sql")
    assert final["before_id"] != final["after_id"]
    assert (project / "models/staging/stg_payments.sql").read_bytes() == source_code
    assert investigator.state(run_id) == final


class ContractProvider:
    def __init__(self, cause: str, model_path: str | None = None, patch: str | None = None) -> None:
        self.cause = cause
        self.model_path = model_path
        self.patch = patch

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        contract = CAUSES[self.cause]
        seen = {item["probe"] for item in evidence if item.get("error") is None}
        for probe in (contract.supporting_probe, contract.competing_probe):
            if probe not in seen:
                return Decision("probe", probe=probe, tokens=1)
        if contract.fix_required:
            patch = self.patch or (
                model_code.rstrip()
                + "\nqualify row_number() over (partition by p.id order by p.event_id) = 1\n"
            )
            return Decision(
                "patch",
                model_path=self.model_path or "models/staging/stg_payments.sql",
                new_code=patch,
                tokens=1,
                cause=self.cause,
                outcome="patched",
                support_probe=contract.supporting_probe,
                competing_probe=contract.competing_probe,
            )
        return Decision(
            "conclude",
            tokens=1,
            cause=self.cause,
            outcome=contract.outcome,
            support_probe=contract.supporting_probe,
            competing_probe=contract.competing_probe,
        )


class PrematureConclusionProvider:
    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        return Decision("conclude", tokens=1, cause="none", outcome="verified_clean")


class UnrefutedConclusionProvider:
    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        if not evidence:
            return Decision("probe", probe="revenue_reconciliation", tokens=1)
        return Decision("conclude", tokens=1, cause="none", outcome="verified_clean")


@pytest.mark.parametrize("provider", [PrematureConclusionProvider, UnrefutedConclusionProvider])
def test_no_fix_conclusion_requires_support_and_competing_query(
    provider: type[PrematureConclusionProvider] | type[UnrefutedConclusionProvider],
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "runs"
    generated = generate("clean", 7, source_root / "incident")
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    investigator = Investigator(tools, provider(), tmp_path / "checkpoints.sqlite")
    final = investigator.run(investigator.start(generated.project))
    assert final["status"] == "failed"
    assert final["conclusion"] is None
    assert final["reasons"] == ["Query evidence is insufficient"]


@pytest.mark.parametrize(
    "scenario",
    [
        "clean",
        "duplicate_payments",
        "join_cardinality",
        "late_refunds",
        "missing_currency_conversion",
        "schema_drift",
    ],
)
def test_graph_accepts_each_evidence_backed_outcome(scenario: str, tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, 7, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    path = "models/staging/stg_payments.sql"
    patch = None
    if scenario == "join_cardinality":
        path = "models/orders.sql"
        patch = (TEMPLATE / path).read_text(encoding="utf-8")
    elif scenario == "missing_currency_conversion":
        patch = (TEMPLATE / path).read_text(encoding="utf-8")
    elif scenario == "schema_drift":
        patch = (
            (TEMPLATE / path)
            .read_text(encoding="utf-8")
            .replace("p.amount / 100.0", "p.gross_cents / 100.0")
        )
    provider = ContractProvider(truth.cause, path, patch)
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    investigator = Investigator(tools, provider, tmp_path / "checkpoints.sqlite")
    final = investigator.run(investigator.start(generated.project))
    result = score_case(truth, final)
    assert result.diagnosis_correct and result.evidence_quality, (
        final["reasons"],
        final["evidence"],
    )
    if truth.fix_required:
        assert final["status"] == "ready_for_review", final["reasons"]
        assert result.fix_correct is True, final["comparison"]
    else:
        assert final["status"] == CAUSES[truth.cause].outcome, final["reasons"]
        assert result.no_fix_handled_correctly is True


def test_pause_resume_and_cancel_survive_new_instance(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path)
    run_id = investigator.start(project)
    assert investigator.pause(run_id)["status"] == "paused"
    restarted = Investigator(investigator.tools, FakeProvider(), investigator.checkpoint_path)
    assert restarted.state(run_id)["status"] == "paused"
    assert restarted.advance(run_id)["steps"] == 1
    assert restarted.resume(run_id)["status"] == "running"
    assert restarted.advance(run_id)["steps"] == 2
    assert restarted.cancel(run_id)["status"] == "cancelled"
    assert investigator.state(run_id)["status"] == "cancelled"
    assert investigator.advance(run_id)["steps"] == 2


class FailingOnceTools(InvestigatorTools):
    failed = False

    def run_sql(
        self, sandbox_id: str, sql: str, row_limit: int = 100, timeout_seconds: int = 10
    ) -> SQLResult:
        if not self.failed:
            self.failed = True
            raise ToolError("Transient query error")
        return super().run_sql(sandbox_id, sql, row_limit, timeout_seconds)


def test_query_error_is_recorded_and_retry_is_bounded(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path, tools_type=FailingOnceTools)
    result = investigator.run(investigator.start(project))
    assert result["status"] == "ready_for_review", result["reasons"]
    assert result["evidence"][0]["error"] == "Transient query error"
    assert result["hypotheses"]["duplicate_payment_ids"]["evidence_id"] == 1


class AlwaysFailTools(InvestigatorTools):
    def run_sql(
        self, sandbox_id: str, sql: str, row_limit: int = 100, timeout_seconds: int = 10
    ) -> SQLResult:
        raise ToolError("Persistent query error")


class UnreadableModelTools(InvestigatorTools):
    def read_model_code(self, sandbox_id: str, _model_path: str) -> str:
        raise ToolError("Model read failed")


def test_model_read_failure_is_recorded(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path, tools_type=UnreadableModelTools)
    result = investigator.run(investigator.start(project))
    assert result["status"] == "failed"
    assert result["reasons"] == ["Model read failed"]
    assert result["events"][-1] == {
        "step": 2,
        "tool": "read_model_code",
        "result": "failed",
        "detail": "Model read failed",
    }


def test_persistent_query_error_stops_after_two_attempts(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path, tools_type=AlwaysFailTools)
    result = investigator.run(investigator.start(project))
    assert result["status"] == "failed"
    assert result["reasons"] == ["Probe retry limit reached"]
    assert len(result["evidence"]) == 2


def test_real_provider_uses_structured_responses_and_usage() -> None:
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        output_text='{"action":"probe","probe":"duplicate_payment_ids",'
        '"model_path":null,"new_code":null}',
        usage=SimpleNamespace(total_tokens=42),
    )
    provider = OpenAIProvider("test-model", cast(OpenAI, client))
    decision = provider.decide([], "select 1")
    assert decision == Decision("probe", "duplicate_payment_ids", tokens=42)
    request = client.responses.create.call_args.kwargs
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert request["model"] == "test-model"


def test_real_provider_can_return_a_verified_no_fix_candidate() -> None:
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        output_text='{"action":"conclude","probe":null,"model_path":null,'
        '"new_code":null,"cause":"delay_refund_ingestion",'
        '"outcome":"verified_no_fix","support_probe":"pending_refunds",'
        '"competing_probe":"refund_model_error"}',
        usage=SimpleNamespace(total_tokens=37),
    )
    provider = OpenAIProvider("test-model", cast(OpenAI, client))
    assert provider.decide([], "select 1") == Decision(
        "conclude",
        tokens=37,
        cause="delay_refund_ingestion",
        outcome="verified_no_fix",
        support_probe="pending_refunds",
        competing_probe="refund_model_error",
    )
    request = client.responses.create.call_args.kwargs
    assert "pending_refunds" in request["text"]["format"]["schema"]["properties"]["probe"]["enum"]


def test_local_provider_uses_only_loopback_and_frozen_limits() -> None:
    response = Mock()
    response.json.return_value = json.loads(
        b'{"message":{"content":"{\\"action\\":\\"probe\\",'
        b'\\"probe\\":\\"pending_refunds\\"}"},'
        b'"prompt_eval_count":30,"eval_count":12}'
    )
    with patch(
        "ai_data_reliability.investigator.provider.httpx.post", return_value=response
    ) as send:
        decision = OllamaLocalProvider().decide([], "select 1")
    assert decision == Decision("probe", "pending_refunds", tokens=42)
    assert send.call_args.args[0] == "http://127.0.0.1:11434/api/chat"
    assert send.call_args.kwargs["trust_env"] is False
    body = send.call_args.kwargs["json"]
    assert body["model"] == "dre-agent-3b"
    assert body["options"] == {
        "num_ctx": 8192,
        "temperature": 0,
        "seed": 7,
        "num_predict": 768,
    }
    assert body["stream"] is False
    assert body["keep_alive"] == "10m"


class InvalidProvider:
    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        return Decision(
            "patch",
            model_path="models/staging/stg_payments.sql",
            new_code=model_code + "\n-- invalid",
            tokens=1,
        )


def test_patch_requires_query_evidence(tmp_path: Path) -> None:
    investigator, project = _investigator(tmp_path)
    investigator.provider = InvalidProvider()
    result = investigator.run(investigator.start(project))
    assert result["status"] == "failed"
    assert result["reasons"] == ["Query evidence is insufficient"]


@pytest.mark.parametrize(
    "steps,tokens,reason",
    [
        (1, 1000, "Step budget exceeded"),
        (10, 1, "Token budget exceeded"),
    ],
)
def test_budgets_stop_work(tmp_path: Path, steps: int, tokens: int, reason: str) -> None:
    investigator, project = _investigator(tmp_path)
    result = investigator.run(investigator.start(project, max_steps=steps, max_tokens=tokens))
    assert result["status"] == "exhausted"
    assert result["reasons"] == [reason]

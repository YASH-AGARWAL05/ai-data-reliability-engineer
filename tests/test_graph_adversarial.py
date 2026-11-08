"""Adversarial decisions against the real checkpointed graph with controlled tool results."""

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from ai_data_reliability.analytics.scenarios import TEMPLATE, generate
from ai_data_reliability.evaluation.contracts import CAUSES
from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import Investigator
from ai_data_reliability.investigator.probes import PROBES
from ai_data_reliability.investigator.provider import Decision, FakeProvider
from ai_data_reliability.investigator.sandbox import (
    DbtCheck,
    InvestigatorTools,
    MetricComparison,
    MetricSnapshot,
    SandboxHandle,
    SQLResult,
    ToolError,
)

SCENARIOS = (
    "clean",
    "duplicate_payments",
    "join_cardinality",
    "late_refunds",
    "missing_currency_conversion",
    "schema_drift",
)
MODES = (
    "one_query",
    "no_link",
    "trivial_competitor",
    "test_edit",
    "config_edit",
    "collateral",
    "finish",
    "claim_clean",
    "invent_defect",
    "late_patch",
    "repeat_failure",
)


class ControlledTools(InvestigatorTools):
    def __init__(
        self,
        scenario: str,
        cause: str,
        clean_net: Decimal,
        *,
        collateral: bool = False,
        fail_queries: bool = False,
        positive_competitor: bool = False,
        candidate_tests_fail: bool = False,
        baseline_tests_fail: bool = False,
    ) -> None:
        self.scenario = scenario
        self.cause = cause
        self.clean_net = clean_net
        self.collateral = collateral
        self.fail_queries = fail_queries
        self.positive_competitor = positive_competitor
        self.candidate_tests_fail = candidate_tests_fail
        self.baseline_tests_fail = baseline_tests_fail
        self.test_calls = 0
        self.sandbox_ids: list[str] = []
        self.source_projects: list[Path] = []
        self.patch_codes: list[str] = []

    def create_sandbox(self, source_project: Path) -> SandboxHandle:
        sandbox_id = str(uuid.uuid4())
        self.sandbox_ids.append(sandbox_id)
        self.source_projects.append(source_project)
        return SandboxHandle(sandbox_id, source_project)

    def _require_sandbox(self, sandbox_id: str, *, candidate: bool = False) -> None:
        if sandbox_id not in self.sandbox_ids or (
            candidate and (len(self.sandbox_ids) < 2 or sandbox_id != self.sandbox_ids[-1])
        ):
            raise ToolError("Wrong sandbox used")

    def read_model_code(self, sandbox_id: str, model_path: str) -> str:
        self._require_sandbox(sandbox_id)
        return "select 1"

    def run_dbt_tests(self, sandbox_id: str) -> DbtCheck:
        self._require_sandbox(sandbox_id)
        self.test_calls += 1
        return DbtCheck(
            (),
            not (self.baseline_tests_fail and self.test_calls == 1)
            and not (self.candidate_tests_fail and self.test_calls > 1),
        )

    def run_sql(
        self, sandbox_id: str, sql: str, row_limit: int = 100, timeout_seconds: int = 10
    ) -> SQLResult:
        self._require_sandbox(sandbox_id)
        if self.fail_queries:
            raise ToolError("query failed")
        probe = next(name for name, query in PROBES.items() if query == sql)
        contract = CAUSES[self.cause]
        rows: tuple[tuple[object, ...], ...] = ()
        if probe == contract.supporting_probe and contract.support_has_rows:
            rows = (("support",),)
        if probe == "revenue_reconciliation" and self.scenario != "clean":
            rows = (("mismatch",),)
        if probe == "pending_refunds" and self.scenario == "late_refunds":
            rows = (("pending",),)
        if probe == contract.competing_probe and self.positive_competitor:
            rows = (("alternative-supported",),)
        return SQLResult(("finding",), rows, False)

    def apply_patch_in_sandbox(self, sandbox_id: str, model_path: str, new_code: str) -> str:
        self._require_sandbox(sandbox_id, candidate=True)
        if not model_path.startswith("models/") or not model_path.endswith(".sql"):
            raise ToolError("Only existing dbt model SQL files may be edited")
        self.patch_codes.append(new_code)
        return f"--- a/{model_path}\n+++ b/{model_path}\n+{new_code}\n"

    def compare_metrics(self, before_id: str, after_id: str) -> MetricComparison:
        self._require_sandbox(before_id)
        self._require_sandbox(after_id, candidate=True)
        if before_id == after_id:
            raise ToolError("Comparison requires two sandboxes")
        snapshot = MetricSnapshot(self.clean_net, self.clean_net, 99, 99, (), 0)
        reasons = ("Unrelated models changed: customers",) if self.collateral else ()
        return MetricComparison(snapshot, snapshot, not self.collateral, reasons)


class MisbehavingProvider:
    def __init__(self, cause: str, mode: str) -> None:
        self.cause = cause
        self.mode = mode

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        contract = CAUSES[self.cause]
        support = contract.supporting_probe
        competitor = contract.competing_probe
        if self.mode == "no_support_no_evidence_patch":
            return Decision(
                "patch",
                model_path="models/staging/stg_payments.sql",
                new_code="select 1",
                tokens=1,
                outcome="patched",
            )
        if self.mode == "finish":
            return Decision("finish", tokens=1)
        if self.mode == "repeat_failure":
            return Decision("probe", probe=support, tokens=1)
        if self.mode == "claim_clean":
            support, competitor = CAUSES["none"].supporting_probe, CAUSES["none"].competing_probe
        if self.mode in {"invent_defect", "late_patch"}:
            support, competitor = (
                CAUSES["duplicate_payment_event"].supporting_probe,
                CAUSES["duplicate_payment_event"].competing_probe,
            )
        if self.mode == "trivial_competitor":
            competitor = next(
                name for name in PROBES if name not in {support, competitor, "pending_refunds"}
            )
        seen = {item["probe"] for item in evidence if item.get("error") is None}
        if self.mode != "no_link" and support not in seen:
            return Decision("probe", probe=support, tokens=1)
        if self.mode not in {"one_query", "no_link"} and competitor not in seen:
            return Decision("probe", probe=competitor, tokens=1)
        reported_competitor = (
            "irrelevant_probe" if self.mode == "wrong_named_competitor" else competitor
        )
        cause = "none" if self.mode == "claim_clean" else self.cause
        if self.mode in {"disguised_finish", "conclude_on_fix", "disguised_patch"}:
            return Decision(
                "conclude" if self.mode == "conclude_on_fix" else "finish",
                tokens=1,
                cause=cause,
                outcome=contract.outcome,
                support_probe=support,
                competing_probe=reported_competitor,
                model_path="models/staging/stg_payments.sql"
                if self.mode == "disguised_patch"
                else None,
                new_code="select 1" if self.mode == "disguised_patch" else None,
            )
        if self.mode == "invent_defect":
            cause = "duplicate_payment_event"
        if self.mode == "late_patch":
            support, competitor = contract.supporting_probe, contract.competing_probe
        if self.mode == "wrong_support":
            support = "pending_refunds"
        elif self.mode == "unknown_support":
            support = "irrelevant_probe"
        if self.mode == "claim_clean" or (
            not contract.fix_required
            and self.mode
            not in {
                "one_query",
                "test_edit",
                "config_edit",
                "collateral",
                "invent_defect",
                "late_patch",
            }
        ):
            return Decision(
                "conclude",
                tokens=1,
                cause=cause,
                outcome="verified_clean" if cause == "none" else "verified_no_fix",
                support_probe=support,
                competing_probe=reported_competitor,
            )
        path: str | None = "models/staging/stg_payments.sql"
        if self.mode == "test_edit":
            path = "tests/assert_revenue_reconciles.sql"
        elif self.mode == "config_edit":
            path = "dbt_project.yml"
        elif self.mode == "missing_model_path":
            path = None
        return Decision(
            "patch",
            model_path=path,
            new_code=model_code + " where 1 = 1",
            tokens=1,
            cause=cause,
            outcome="patched",
            support_probe=support,
            competing_probe=reported_competitor,
        )


@pytest.mark.parametrize(
    "scenario,mode",
    [
        (scenario, mode)
        for scenario in SCENARIOS
        for mode in MODES
        if (mode != "claim_clean" or scenario != "clean")
        and (mode != "invent_defect" or scenario == "clean")
        and (mode != "late_patch" or scenario == "late_refunds")
    ],
)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_adversarial_graph_rejects(scenario: str, seed: int, mode: str, tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, seed, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(
        scenario,
        truth.cause,
        truth.clean_baseline.net_revenue_usd,
        collateral=mode == "collateral",
        fail_queries=mode == "repeat_failure",
    )
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, mode), tmp_path / "checkpoints.sqlite"
    )
    final = investigator.run(investigator.start(generated.project))
    assert final["status"] in {"failed", "exhausted"}, (scenario, seed, mode, final["reasons"])
    assert final["conclusion"] is None
    assert final["steps"] <= final["max_steps"]
    if mode == "repeat_failure":
        assert final["reasons"] == ["Probe retry limit reached"]
        assert len(final["evidence"]) == 2


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_correct_graph_accepts_with_controlled_tools(
    scenario: str, seed: int, tmp_path: Path
) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, seed, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(scenario, truth.cause, truth.clean_baseline.net_revenue_usd)
    provider = MisbehavingProvider(truth.cause, "correct")
    investigator = Investigator(tools, provider, tmp_path / "checkpoints.sqlite")
    final = investigator.run(investigator.start(generated.project))
    result = score_case(truth, final)
    assert result.diagnosis_correct and result.evidence_quality
    assert (
        (result.fix_correct is True)
        if truth.fix_required
        else (result.no_fix_handled_correctly is True)
    )
    assert tools.source_projects == [generated.project.resolve()] * (2 if truth.fix_required else 1)
    assert tools.patch_codes == (["select 1 where 1 = 1"] if truth.fix_required else [])
    support = CAUSES[truth.cause].supporting_probe
    competitor = CAUSES[truth.cause].competing_probe
    support_id = next(
        index for index, item in enumerate(final["evidence"]) if item["probe"] == support
    )
    competitor_id = next(
        index for index, item in enumerate(final["evidence"]) if item["probe"] == competitor
    )
    assert final["hypotheses"][truth.cause] == {
        "status": "verified",
        "evidence_id": support_id,
        "competing_evidence_id": competitor_id,
    }
    if truth.fix_required:
        assert final["hypotheses"][support] == final["hypotheses"][truth.cause]


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_forged_verified_status_without_query_is_rejected(
    scenario: str, seed: int, tmp_path: Path
) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, seed, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(scenario, truth.cause, truth.clean_baseline.net_revenue_usd)
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "no_link"), tmp_path / "checkpoints.sqlite"
    )
    run_id = investigator.start(generated.project)
    state = investigator.state(run_id)
    with investigator._saver() as saver:
        investigator._graph(saver).update_state(
            investigator._config(run_id),
            {"hypotheses": {truth.cause: {"status": "verified", "evidence_id": 0}}},
            as_node="step",
        )
    final = investigator.run(run_id)
    assert state["evidence"] == []
    assert final["status"] == "failed"
    assert final["conclusion"] is None


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_evidence_copied_from_another_run_is_rejected(
    scenario: str, seed: int, tmp_path: Path
) -> None:
    source_root = tmp_path / "runs"
    first = generate(scenario, seed, source_root / "first")
    second = generate(scenario, seed, source_root / "second")
    truth = GroundTruth.model_validate_json(first.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(scenario, truth.cause, truth.clean_baseline.net_revenue_usd)
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "correct"), tmp_path / "checkpoints.sqlite"
    )
    first_id = investigator.start(first.project)
    investigator.advance(first_id)
    foreign = investigator.state(first_id)["evidence"][0]
    second_id = investigator.start(second.project)
    investigator.advance(second_id)
    investigator.advance(second_id)
    second_state = investigator.state(second_id)
    assert len(second_state["evidence"]) == 2
    with investigator._saver() as saver:
        investigator._graph(saver).update_state(
            investigator._config(second_id),
            {"evidence": [foreign, second_state["evidence"][1]]},
            as_node="step",
        )
    final = investigator.run(second_id)
    assert final["status"] == "failed"
    assert final["conclusion"] is None


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_supported_competing_explanation_blocks_verification(
    scenario: str, seed: int, tmp_path: Path
) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, seed, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(
        scenario, truth.cause, truth.clean_baseline.net_revenue_usd, positive_competitor=True
    )
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "correct"), tmp_path / "checkpoints.sqlite"
    )
    final = investigator.run(investigator.start(generated.project))
    assert final["status"] == "failed"
    assert final["conclusion"] is None


@pytest.mark.parametrize(
    "scenario",
    ["duplicate_payments", "join_cardinality", "missing_currency_conversion", "schema_drift"],
)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_failed_candidate_regression_check_blocks_patch(
    scenario: str, seed: int, tmp_path: Path
) -> None:
    source_root = tmp_path / "runs"
    generated = generate(scenario, seed, source_root / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(
        scenario, truth.cause, truth.clean_baseline.net_revenue_usd, candidate_tests_fail=True
    )
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "correct"), tmp_path / "checkpoints.sqlite"
    )
    final = investigator.run(investigator.start(generated.project))
    assert tools.test_calls == 2
    assert final["status"] == "failed"
    assert final["conclusion"] is None
    assert final["reasons"] == ["Patch dbt tests failed"]


@pytest.mark.parametrize("scenario", ["clean", "late_refunds"])
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_no_fix_requires_passing_baseline_tests(scenario: str, seed: int, tmp_path: Path) -> None:
    generated = generate(scenario, seed, tmp_path / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(
        scenario, truth.cause, truth.clean_baseline.net_revenue_usd, baseline_tests_fail=True
    )
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "correct"), tmp_path / "checkpoints.sqlite"
    )
    final = investigator.run(investigator.start(generated.project))
    assert tools.test_calls == 1
    assert final["status"] == "failed"
    assert final["conclusion"] is None


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_scripted_provider_exercises_each_outcome(scenario: str, seed: int, tmp_path: Path) -> None:
    generated = generate(scenario, seed, tmp_path / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(scenario, truth.cause, truth.clean_baseline.net_revenue_usd)
    investigator = Investigator(
        tools, FakeProvider(scenario, TEMPLATE), tmp_path / "checkpoints.sqlite"
    )
    final = investigator.run(investigator.start(generated.project))
    result = score_case(truth, final)
    assert result.diagnosis_correct and result.evidence_quality
    if truth.fix_required:
        assert result.fix_correct is True
    else:
        assert result.no_fix_handled_correctly is True

"""Adversarial checkpoint values at the evidence acceptance boundary."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_graph_adversarial import ControlledTools, MisbehavingProvider

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.evaluation.contracts import CAUSES
from ai_data_reliability.evaluation.scoring import GroundTruth, _model_paths_only, score_case
from ai_data_reliability.investigator.graph import (
    InvestigationState,
    Investigator,
    _evidence_pair,
    _linked_rows,
)
from ai_data_reliability.investigator.probes import PLANS, PROBES
from ai_data_reliability.investigator.provider import FakeProvider


def _state() -> InvestigationState:
    support = "duplicate_payment_ids"
    competitor = "currency_join_cardinality"
    return cast(
        InvestigationState,
        {
            "run_id": "run-one",
            "before_id": "sandbox-one",
            "evidence": [
                {
                    "id": 0,
                    "run_id": "run-one",
                    "sandbox_id": "sandbox-one",
                    "probe": support,
                    "sql": PROBES[support],
                    "rows": [["pay-1", 2]],
                    "error": None,
                },
                {
                    "id": 1,
                    "run_id": "run-one",
                    "sandbox_id": "sandbox-one",
                    "probe": competitor,
                    "sql": PROBES[competitor],
                    "rows": [],
                    "error": None,
                },
            ],
            "hypotheses": {
                support: {"status": "supported", "evidence_id": 0},
                competitor: {"status": "refuted", "evidence_id": 1},
            },
        },
    )


def test_checkpoint_lifecycle_is_selected_for_mutation(tmp_path: Path) -> None:
    source = generate("clean", 7, tmp_path / "runs" / "incident")
    tools = ControlledTools("clean", "none", Decimal("1736.85"))
    investigator = Investigator(
        tools, MisbehavingProvider("none", "finish"), tmp_path / "checkpoints.sqlite"
    )
    run_id = investigator.start(source.project)
    assert investigator.pause(run_id)["status"] == "paused"
    assert investigator.pause(run_id)["status"] == "paused"
    restarted = Investigator(
        tools, MisbehavingProvider("none", "finish"), investigator.checkpoint_path
    )
    assert restarted.state(run_id)["status"] == "paused"
    assert restarted.resume(run_id)["status"] == "running"
    assert restarted.cancel(run_id)["status"] == "cancelled"
    assert investigator.state(run_id)["status"] == "cancelled"
    assert investigator.pause(run_id)["status"] == "cancelled"
    assert restarted.resume(run_id)["status"] == "running"
    assert restarted.run(run_id)["status"] == "failed"
    with pytest.raises(ValueError, match="paused or cancelled"):
        restarted.resume(run_id)


def test_linked_query_pair_requires_tool_result_and_refuted_alternative() -> None:
    state = _state()
    assert _linked_rows(state, "duplicate_payment_ids", "supported") == (0, [["pay-1", 2]])
    assert _linked_rows(state, "currency_join_cardinality", "refuted") == (1, [])
    assert _evidence_pair(state, "duplicate_payment_ids", PLANS["duplicate_payment_ids"]) == (
        0,
        1,
    )

    supported_competitor = deepcopy(state)
    supported_competitor["evidence"][1]["rows"] = [["USD", 2]]
    assert (
        _evidence_pair(
            supported_competitor, "duplicate_payment_ids", PLANS["duplicate_payment_ids"]
        )
        is None
    )

    empty_support = deepcopy(state)
    empty_support["evidence"][0]["rows"] = []
    assert (
        _evidence_pair(empty_support, "duplicate_payment_ids", PLANS["duplicate_payment_ids"])
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        "wrong_status",
        "missing_status",
        "out_of_range",
        "failed_query",
        "non_list_rows",
        "other_run",
        "other_sandbox",
        "other_sql",
        "other_probe",
    ],
)
def test_linked_rows_rejects_forged_or_invalid_record(change: str) -> None:
    state = _state()
    hypothesis = state["hypotheses"]["duplicate_payment_ids"]
    item = state["evidence"][0]
    if change == "wrong_status":
        hypothesis["status"] = "verified"
    elif change == "missing_status":
        del hypothesis["status"]
    elif change == "out_of_range":
        hypothesis["evidence_id"] = len(state["evidence"])
    elif change == "failed_query":
        item["error"] = "warehouse error"
    elif change == "non_list_rows":
        item["rows"] = ("row",)
    elif change == "other_run":
        item["run_id"] = "run-two"
    elif change == "other_sandbox":
        item["sandbox_id"] = "sandbox-two"
    elif change == "other_sql":
        item["sql"] = "select 1 where false"
    elif change == "other_probe":
        item["probe"] = "pending_refunds"
    assert _linked_rows(state, "duplicate_payment_ids", "supported") is None


@pytest.mark.parametrize(
    "paths",
    [
        [],
        "models/orders.sql",
        ("models/orders.sql",),
        [None],
        ["/models/orders.sql"],
        ["models/../tests/assert.sql"],
        ["models/../orders.sql"],
        ["tests/assert.sql"],
        ["dbt_project.yml"],
        ["models/orders.yml"],
        ["models/tests/assert.sql"],
        ["models/test/assert.sql"],
        ["models/macros/override.sql"],
        ["models/seeds/raw.sql"],
        ["models/orders.sql", "tests/assert.sql"],
    ],
)
def test_scorer_rejects_non_model_patch_paths(paths: object) -> None:
    assert _model_paths_only(paths) is False


def test_scorer_accepts_only_model_sql_paths() -> None:
    assert _model_paths_only(["models/orders.sql", "models/staging/stg_payments.sql"])


@pytest.mark.parametrize(
    "scenario,mode,accepted",
    [
        ("duplicate_payments", "correct", True),
        ("duplicate_payments", "one_query", False),
        ("duplicate_payments", "trivial_competitor", False),
        ("duplicate_payments", "wrong_named_competitor", False),
        ("duplicate_payments", "wrong_support", False),
        ("duplicate_payments", "unknown_support", False),
        ("duplicate_payments", "missing_model_path", False),
        ("duplicate_payments", "disguised_patch", False),
        ("duplicate_payments", "no_support_no_evidence_patch", False),
        ("duplicate_payments", "repeat_failure", False),
        ("duplicate_payments", "conclude_on_fix", False),
        ("duplicate_payments", "disguised_finish", False),
        ("duplicate_payments", "collateral", False),
        ("duplicate_payments", "candidate_failure", False),
        ("join_cardinality", "correct", True),
        ("missing_currency_conversion", "correct", True),
        ("schema_drift", "correct", True),
        ("late_refunds", "correct", True),
        ("late_refunds", "late_patch", False),
        ("late_refunds", "trivial_competitor", False),
        ("late_refunds", "wrong_named_competitor", False),
        ("clean", "correct", True),
        ("clean", "finish", False),
        ("clean", "disguised_finish", False),
        ("clean", "invent_defect", False),
        ("clean", "trivial_competitor", False),
        ("clean", "wrong_named_competitor", False),
        ("clean", "unknown_support", False),
        ("clean", "baseline_failure", False),
    ],
)
def test_compact_gate_mutation_cases(
    scenario: str, mode: str, accepted: bool, tmp_path: Path
) -> None:
    generated = generate(scenario, 7, tmp_path / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools(
        scenario,
        truth.cause,
        truth.clean_baseline.net_revenue_usd,
        collateral=mode == "collateral",
        fail_queries=mode == "repeat_failure",
        candidate_tests_fail=mode == "candidate_failure",
        baseline_tests_fail=mode == "baseline_failure",
    )
    investigator = Investigator(
        tools,
        MisbehavingProvider(
            truth.cause,
            "correct" if mode in {"candidate_failure", "baseline_failure"} else mode,
        ),
        tmp_path / "checkpoints.sqlite",
    )
    final = investigator.run(investigator.start(generated.project))
    if accepted:
        result = score_case(truth, final)
        assert result.diagnosis_correct and result.evidence_quality
        if truth.fix_required:
            assert result.fix_correct is True
        else:
            assert result.no_fix_handled_correctly is True
        assert tools.source_projects == [generated.project.resolve()] * (
            2 if truth.fix_required else 1
        )
        assert tools.patch_codes == (["select 1 where 1 = 1"] if truth.fix_required else [])
        if truth.fix_required:
            assert final["after_id"] == tools.sandbox_ids[1]
            assert final["diff"].startswith("--- a/models/staging/stg_payments.sql")
            assert final["comparison"] is not None
            assert final["comparison"]["eligible_for_review"] is True
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
    else:
        assert final["status"] == "failed"
        assert final["conclusion"] is None
        if mode == "collateral":
            assert final["reasons"] == ["Unrelated models changed: customers"]
            assert final["after_id"] == tools.sandbox_ids[1]
            assert final["diff"].startswith("--- a/models/staging/stg_payments.sql")
            assert final["comparison"] is not None
            assert final["comparison"]["eligible_for_review"] is False
        elif mode == "candidate_failure":
            assert final["reasons"] == ["Patch dbt tests failed"]
            assert final["after_id"] == tools.sandbox_ids[1]
            assert final["diff"].startswith("--- a/models/staging/stg_payments.sql")
            assert final["comparison"] is not None
        elif mode == "repeat_failure":
            assert final["reasons"] == ["Probe retry limit reached"]
            assert len(final["evidence"]) == 2
            assert all(item["error"] == "query failed" for item in final["evidence"])


def test_legacy_provider_patch_still_requires_unique_evidence(tmp_path: Path) -> None:
    generated = generate("duplicate_payments", 7, tmp_path / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools("duplicate_payments", truth.cause, truth.clean_baseline.net_revenue_usd)
    investigator = Investigator(tools, FakeProvider(), tmp_path / "checkpoints.sqlite")
    final = investigator.run(investigator.start(generated.project))
    assert final["status"] == "ready_for_review"
    assert final["hypotheses"]["duplicate_payment_ids"]["status"] == "verified"
    assert final["hypotheses"]["currency_join_cardinality"]["status"] == "refuted"


def test_no_fix_refuses_dirty_checkpoint(tmp_path: Path) -> None:
    generated = generate("clean", 7, tmp_path / "incident")
    truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
    tools = ControlledTools("clean", truth.cause, truth.clean_baseline.net_revenue_usd)
    investigator = Investigator(
        tools, MisbehavingProvider(truth.cause, "correct"), tmp_path / "checkpoints.sqlite"
    )
    run_id = investigator.start(generated.project)
    for _ in range(2):
        investigator.advance(run_id)
    state = investigator.state(run_id)
    assert len(state["evidence"]) == 2
    assert state["status"] == "running"
    with investigator._saver() as saver:
        investigator._graph(saver).update_state(
            investigator._config(run_id),
            {"diff": "+unverified edit", "changed_paths": ["models/orders.sql"]},
            as_node="step",
        )
    final = investigator.run(run_id)
    assert final["status"] == "failed"
    assert final["conclusion"] is None

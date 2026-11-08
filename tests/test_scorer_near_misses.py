"""Expected outcomes transcribed from scenario definitions, not evaluator contracts."""

from copy import deepcopy
from decimal import Decimal
from typing import cast

import pytest

from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import InvestigationState
from ai_data_reliability.investigator.probes import PROBES

# Values were checked against clean scenario generation for seeds 7, 8, and 9.
BASELINES = {7: "1736.85", 8: "1776.45", 9: "1749.40"}
CASES = (
    ("clean", "none", False, "verified_clean", "revenue_reconciliation", "pending_refunds", False),
    (
        "duplicate_payments",
        "duplicate_payment_event",
        True,
        "patched",
        "duplicate_payment_ids",
        "currency_join_cardinality",
        True,
    ),
    (
        "join_cardinality",
        "multiply_payment_join",
        True,
        "patched",
        "payment_join_multiplication",
        "duplicate_payment_ids",
        True,
    ),
    (
        "late_refunds",
        "delay_refund_ingestion",
        False,
        "verified_no_fix",
        "pending_refunds",
        "refund_model_error",
        True,
    ),
    (
        "missing_currency_conversion",
        "omit_fx_conversion",
        True,
        "patched",
        "missing_fx_conversion",
        "currency_join_cardinality",
        True,
    ),
    (
        "schema_drift",
        "rename_payment_amount",
        True,
        "patched",
        "payment_amount_schema",
        "duplicate_payment_ids",
        True,
    ),
)


def _case(
    scenario: str,
    cause: str,
    fix_required: bool,
    outcome: str,
    support_probe: str,
    competitor_probe: str,
    support_has_rows: bool,
    seed: int,
) -> tuple[GroundTruth, InvestigationState]:
    truth = GroundTruth.model_validate(
        {
            "scenario": scenario,
            "seed": seed,
            "cause": cause,
            "fix_required": fix_required,
            "clean_baseline": {"net_revenue_usd": BASELINES[seed], "order_count": 99},
        }
    )
    state = cast(
        InvestigationState,
        {
            "run_id": "test-run",
            "source_project": "/fixture/project",
            "status": "ready_for_review" if fix_required else outcome,
            "steps": 4,
            "tokens": 75,
            "max_steps": 10,
            "max_tokens": 1000,
            "before_id": "before",
            "after_id": "after" if fix_required else "",
            "evidence": [
                {
                    "id": 0,
                    "run_id": "test-run",
                    "sandbox_id": "before",
                    "probe": support_probe,
                    "sql": PROBES[support_probe],
                    "rows": [["finding"]] if support_has_rows else [],
                    "error": None,
                },
                {
                    "id": 1,
                    "run_id": "test-run",
                    "sandbox_id": "before",
                    "probe": competitor_probe,
                    "sql": PROBES[competitor_probe],
                    "rows": [],
                    "error": None,
                },
            ],
            "events": [],
            "hypotheses": {
                cause: {"status": "verified", "evidence_id": 0},
                competitor_probe: {"status": "refuted", "evidence_id": 1},
            },
            "conclusion": {"cause": cause, "outcome": outcome},
            "changed_paths": ["models/fct_daily_revenue.sql"] if fix_required else [],
            "diff": "+verified model repair" if fix_required else "",
            "reasons": [],
            "comparison": {
                "eligible_for_review": True,
                "reasons": [],
                "after": {"actual_net_usd": BASELINES[seed], "order_count": 99},
            }
            if fix_required
            else None,
            "baseline_tests_passed": not fix_required,
        },
    )
    return truth, state


@pytest.mark.parametrize("seed", [7, 8, 9])
@pytest.mark.parametrize("case", CASES, ids=[row[0] for row in CASES])
def test_explicit_perfect_and_near_miss_scores(
    case: tuple[str, str, bool, str, str, str, bool], seed: int
) -> None:
    truth, perfect = _case(*case, seed)
    score = score_case(truth, perfect)
    assert score.diagnosis_correct and score.evidence_quality
    assert score.expected_cause == truth.cause
    assert score.predicted_cause == truth.cause
    assert score.clean_net_usd == BASELINES[seed]
    if truth.fix_required:
        assert score.fix_correct is True and score.no_fix_handled_correctly is None
        assert score.after_net_usd == BASELINES[seed]
    else:
        assert score.no_fix_handled_correctly is True and score.fix_correct is None
        assert score.after_net_usd is None

    no_evidence = deepcopy(perfect)
    no_evidence["evidence"] = []
    assert not score_case(truth, no_evidence).diagnosis_correct

    for field, value in (
        ("run_id", "another-run"),
        ("sandbox_id", "another-sandbox"),
        ("sql", "select 1 where false"),
        ("error", "query failed"),
    ):
        forged = deepcopy(perfect)
        forged["evidence"][0][field] = value
        assert not score_case(truth, forged).evidence_quality

    unrefuted = deepcopy(perfect)
    unrefuted["evidence"][1]["rows"] = [["alternative"]]
    assert not score_case(truth, unrefuted).evidence_quality

    non_list_rows = deepcopy(perfect)
    non_list_rows["evidence"][0]["rows"] = ("finding",)
    assert not score_case(truth, non_list_rows).evidence_quality

    alternative = next(row for row in CASES if row[1] != truth.cause)
    _, wrong_cause = _case(*alternative, seed)
    wrong_score = score_case(truth, wrong_cause)
    assert not wrong_score.diagnosis_correct
    if truth.scenario == "clean":
        assert wrong_score.false_positive

    unreconciled = deepcopy(perfect)
    unreconciled["diff"] = "+unreconciled patch"
    unreconciled["changed_paths"] = ["models/fct_daily_revenue.sql"]
    unreconciled["comparison"] = {
        "eligible_for_review": False,
        "reasons": ["Net revenue does not reconcile to raw events"],
        "after": {"actual_net_usd": str(Decimal(BASELINES[seed]) + 1), "order_count": 99},
    }
    failed_score = score_case(truth, unreconciled)
    if truth.fix_required:
        assert failed_score.fix_correct is False
    else:
        assert failed_score.no_fix_handled_correctly is False
    if truth.scenario == "late_refunds":
        assert failed_score.fix_correct is None
    if truth.scenario == "clean":
        assert failed_score.false_positive

        patched_clean = deepcopy(perfect)
        patched_clean["diff"] = "+invented repair"
        patched_clean["changed_paths"] = ["models/orders.sql"]
        assert score_case(truth, patched_clean).false_positive

    if not truth.fix_required:
        comparison_without_patch = deepcopy(perfect)
        comparison_without_patch["comparison"] = {
            "eligible_for_review": True,
            "reasons": [],
            "after": {"actual_net_usd": BASELINES[seed], "order_count": 99},
        }
        assert score_case(truth, comparison_without_patch).no_fix_handled_correctly is False

    if truth.fix_required:
        unexplained_regression = deepcopy(perfect)
        assert unexplained_regression["comparison"] is not None
        unexplained_regression["comparison"]["reasons"] = ["dbt tests did not pass"]
        bad_score = score_case(truth, unexplained_regression)
        assert bad_score.no_collateral_damage is False
        assert bad_score.fix_correct is False

        wrong_count = deepcopy(perfect)
        assert wrong_count["comparison"] is not None
        cast(dict[str, object], wrong_count["comparison"]["after"])["order_count"] = 98
        assert score_case(truth, wrong_count).fix_correct is False

        missing_amount = deepcopy(perfect)
        assert missing_amount["comparison"] is not None
        cast(dict[str, object], missing_amount["comparison"]["after"])["actual_net_usd"] = Decimal(
            BASELINES[seed]
        )
        assert score_case(truth, missing_amount).fix_correct is False

        invalid_amount = deepcopy(perfect)
        assert invalid_amount["comparison"] is not None
        cast(dict[str, object], invalid_amount["comparison"]["after"])["actual_net_usd"] = "invalid"
        assert score_case(truth, invalid_amount).fix_correct is False

        at_tolerance = deepcopy(perfect)
        assert at_tolerance["comparison"] is not None
        cast(dict[str, object], at_tolerance["comparison"]["after"])["actual_net_usd"] = str(
            Decimal(BASELINES[seed]) + Decimal("0.005")
        )
        assert score_case(truth, at_tolerance).fix_correct is True


def test_false_positive_follows_clean_outcome_for_another_scenario_name() -> None:
    truth, clean = _case(*CASES[0], 7)
    truth = truth.model_copy(update={"scenario": "another_clean_control"})
    assert score_case(truth, clean).no_fix_handled_correctly is True

    invented = deepcopy(clean)
    invented["conclusion"] = {"cause": "invented_defect", "outcome": "patched"}
    result = score_case(truth, invented)
    assert result.false_positive
    assert not result.diagnosis_correct


@pytest.mark.parametrize("case", [CASES[0], CASES[3]], ids=["clean", "late_refunds"])
def test_no_fix_rejects_either_patch_signal_alone(
    case: tuple[str, str, bool, str, str, str, bool],
) -> None:
    truth, perfect = _case(*case, 7)
    for field, value in (("diff", "+edit"), ("changed_paths", ["models/orders.sql"])):
        patched = deepcopy(perfect)
        cast(dict[str, object], patched)[field] = value
        result = score_case(truth, patched)
        assert result.no_fix_handled_correctly is False
        if truth.scenario == "clean":
            assert result.false_positive


def test_malformed_conclusion_is_not_credited() -> None:
    truth, perfect = _case(*CASES[1], 7)
    malformed_values: tuple[object, ...] = ([], {"cause": 17, "outcome": "patched"})
    for malformed in malformed_values:
        state = cast(InvestigationState, {**perfect, "conclusion": malformed})
        result = score_case(truth, state)
        assert result.predicted_cause is None
        assert not result.diagnosis_correct
        assert result.fix_correct is False


def test_conflicting_ground_truth_is_rejected() -> None:
    truth, perfect = _case(*CASES[1], 7)
    conflicting = truth.model_copy(update={"fix_required": False})
    with pytest.raises(ValueError, match="^Ground truth conflicts with the cause contract$"):
        score_case(conflicting, perfect)

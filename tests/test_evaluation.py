import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.evaluation.contracts import CAUSES
from ai_data_reliability.evaluation.report import EvaluationReport, load_reports, summarize
from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import InvestigationState
from ai_data_reliability.investigator.probes import PROBES

SCENARIOS = (
    "clean",
    "duplicate_payments",
    "join_cardinality",
    "late_refunds",
    "missing_currency_conversion",
    "schema_drift",
)


def _truth(tmp_path: Path, scenario: str, seed: int) -> GroundTruth:
    generated = generate(scenario, seed, tmp_path / f"{scenario}-{seed}")
    return GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))


def _perfect(truth: GroundTruth) -> InvestigationState:
    contract = CAUSES[truth.cause]
    support = [["matched"]] if contract.support_has_rows else []
    return cast(
        InvestigationState,
        {
            "run_id": "test-run",
            "source_project": "/fixture/project",
            "status": "ready_for_review" if truth.fix_required else contract.outcome,
            "steps": 4,
            "tokens": 75,
            "max_steps": 10,
            "max_tokens": 1000,
            "before_id": "before",
            "after_id": "after" if truth.fix_required else "",
            "evidence": [
                {
                    "id": 0,
                    "run_id": "test-run",
                    "sandbox_id": "before",
                    "probe": contract.supporting_probe,
                    "sql": PROBES[contract.supporting_probe],
                    "rows": support,
                    "error": None,
                },
                {
                    "id": 1,
                    "run_id": "test-run",
                    "sandbox_id": "before",
                    "probe": contract.competing_probe,
                    "sql": PROBES[contract.competing_probe],
                    "rows": [],
                    "error": None,
                },
            ],
            "events": [],
            "hypotheses": {
                truth.cause: {"status": "verified", "evidence_id": 0},
                contract.competing_probe: {"status": "refuted", "evidence_id": 1},
            },
            "conclusion": {"cause": truth.cause, "outcome": contract.outcome},
            "changed_paths": ["models/staging/stg_payments.sql"] if truth.fix_required else [],
            "diff": "+valid repair" if truth.fix_required else "",
            "reasons": [],
            "comparison": (
                {
                    "eligible_for_review": True,
                    "reasons": [],
                    "after": {
                        "actual_net_usd": str(truth.clean_baseline.net_revenue_usd),
                        "order_count": truth.clean_baseline.order_count,
                    },
                }
                if truth.fix_required
                else None
            ),
        },
    )


def _success(truth: GroundTruth, state: InvestigationState) -> bool:
    result = score_case(truth, state)
    return (
        result.fix_correct is True
        if truth.fix_required
        else result.no_fix_handled_correctly is True
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_perfect_lazy_and_sabotage_outcomes(tmp_path: Path, scenario: str, seed: int) -> None:
    truth = _truth(tmp_path, scenario, seed)
    perfect = _perfect(truth)
    scored = score_case(truth, perfect)
    assert scored.diagnosis_correct and scored.evidence_quality
    assert _success(truth, perfect)
    assert (
        scored.fix_correct is None
        if not truth.fix_required
        else scored.no_fix_handled_correctly is None
    )

    lazy = _perfect(truth)
    lazy["evidence"] = []
    assert not score_case(truth, lazy).diagnosis_correct
    assert not _success(truth, lazy)

    test_edit = _perfect(truth)
    test_edit["changed_paths"] = ["tests/assert_revenue_reconciles.sql"]
    test_edit["diff"] = "+altered test"
    assert not _success(truth, test_edit)

    collateral = _perfect(truth)
    collateral["changed_paths"] = ["models/customers.sql"]
    collateral["diff"] = "+same revenue, changed customer rows"
    collateral["comparison"] = {
        "eligible_for_review": False,
        "reasons": ["Unrelated models changed: customers"],
        "after": {
            "actual_net_usd": str(truth.clean_baseline.net_revenue_usd),
            "order_count": truth.clean_baseline.order_count,
        },
    }
    result = score_case(truth, collateral)
    assert result.no_collateral_damage is False
    assert not _success(truth, collateral)


def test_clean_invented_defect_is_false_positive(tmp_path: Path) -> None:
    truth = _truth(tmp_path, "clean", 7)
    state = _perfect(truth)
    state["conclusion"] = {"cause": "invented_defect", "outcome": "patched"}
    result = score_case(truth, state)
    assert result.false_positive
    assert not result.diagnosis_correct
    assert result.no_fix_handled_correctly is False


def test_malformed_evidence_and_wrong_revenue_are_rejected(tmp_path: Path) -> None:
    truth = _truth(tmp_path, "duplicate_payments", 7)
    state = _perfect(truth)
    state["evidence"][0]["error"] = "query failed"
    assert not score_case(truth, state).evidence_quality

    state = _perfect(truth)
    state["hypotheses"][truth.cause]["evidence_id"] = len(state["evidence"])
    assert not score_case(truth, state).evidence_quality

    state = _perfect(truth)
    comparison = state["comparison"]
    assert comparison is not None
    cast(dict[str, object], comparison["after"])["actual_net_usd"] = "0.00"
    assert score_case(truth, state).fix_correct is False


def test_summary_keeps_fix_and_no_fix_denominators_separate(tmp_path: Path) -> None:
    clean_truth = _truth(tmp_path, "clean", 7)
    repair_truth = _truth(tmp_path, "duplicate_payments", 7)
    clean = score_case(clean_truth, _perfect(clean_truth))
    repaired = score_case(repair_truth, _perfect(repair_truth))
    summary = summarize([clean, repaired])
    assert summary.fix_required_cases == 1 and summary.fix_correct == 1
    assert summary.no_fix_required_cases == 1 and summary.no_fix_handled_correctly == 1
    assert summary.control_cases == 1 and summary.false_positives == 0

    report = EvaluationReport(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        provider="fake",
        agent_version="agent123",
        fixture_version="fixture123",
        contract_version=2,
        seeds=[7],
        scenarios=["clean", "duplicate_payments"],
        complete_suite=False,
        results=[clean, repaired],
        summary=summary,
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / f"{report.id}.json").write_text(report.model_dump_json(), encoding="utf-8")
    assert load_reports(tmp_path) == [report]
    assert report.scripted is True
    legacy = report.model_dump()
    del legacy["scripted"]
    assert EvaluationReport.model_validate(legacy).scripted is True
    legacy["provider"] = "openai"
    assert EvaluationReport.model_validate(legacy).scripted is False

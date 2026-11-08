"""Score explicit conclusions against truth held outside investigator sandboxes."""

from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from pydantic import BaseModel

from ai_data_reliability.evaluation.contracts import CAUSES
from ai_data_reliability.evaluation.report import CaseResult
from ai_data_reliability.investigator.graph import InvestigationState
from ai_data_reliability.investigator.probes import PROBES


class CleanBaseline(BaseModel):
    net_revenue_usd: Decimal
    order_count: int


class GroundTruth(BaseModel):
    scenario: str
    seed: int
    cause: str
    fix_required: bool
    clean_baseline: CleanBaseline


def _linked_rows(
    state: InvestigationState, hypothesis: dict[str, object], probe: str
) -> list[object] | None:
    evidence_id = hypothesis.get("evidence_id")
    if not isinstance(evidence_id, int) or not 0 <= evidence_id < len(state["evidence"]):
        return None
    item = state["evidence"][evidence_id]
    rows = item.get("rows")
    if (
        item.get("probe") != probe
        or item.get("run_id") != state["run_id"]
        or item.get("sandbox_id") != state["before_id"]
        or item.get("sql") != PROBES[probe]
        or item.get("error") is not None
    ):
        return None
    return rows if isinstance(rows, list) else None


def _model_paths_only(paths: object) -> bool:
    if not isinstance(paths, list) or not paths:
        return False
    for value in paths:
        if not isinstance(value, str):
            return False
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.parts[0] != "models"
            or path.suffix != ".sql"
            or any(part in {"test", "tests", "macros", "seeds"} for part in path.parts)
        ):
            return False
    return True


def score_case(truth: GroundTruth, state: InvestigationState) -> CaseResult:
    contract = CAUSES[truth.cause]
    if contract.fix_required != truth.fix_required:
        raise ValueError("Ground truth conflicts with the cause contract")
    conclusion = state.get("conclusion")
    conclusion = conclusion if isinstance(conclusion, dict) else {}
    predicted = conclusion.get("cause")
    predicted = predicted if isinstance(predicted, str) else None
    outcome = conclusion.get("outcome")
    hypothesis = state["hypotheses"].get(predicted or "", {})
    competitor = state["hypotheses"].get(contract.competing_probe, {})
    support = _linked_rows(state, hypothesis, contract.supporting_probe)
    competing = _linked_rows(state, competitor, contract.competing_probe)
    evidence_quality = (
        predicted == truth.cause
        and outcome == contract.outcome
        and hypothesis.get("status") == "verified"
        and support is not None
        and bool(support) == contract.support_has_rows
        and competitor.get("status") == "refuted"
        and competing == []
    )
    diagnosis_correct = predicted == truth.cause and evidence_quality
    comparison = state["comparison"]
    after_net: str | None = None
    no_collateral: bool | None = None
    reconciles = False
    eligible = False
    if comparison is not None:
        after = comparison.get("after")
        if isinstance(after, dict):
            actual = after.get("actual_net_usd")
            if isinstance(actual, str):
                after_net = actual
                with suppress(InvalidOperation):
                    reconciles = abs(
                        Decimal(actual) - truth.clean_baseline.net_revenue_usd
                    ) <= Decimal("0.005")
            reasons = comparison.get("reasons")
            if isinstance(reasons, list):
                no_collateral = (
                    after.get("order_count") == truth.clean_baseline.order_count and reasons == []
                )
            eligible = comparison.get("eligible_for_review") is True
    changed_paths = state.get("changed_paths", [])
    patch_present = bool(state["diff"]) or bool(changed_paths)
    fix_correct: bool | None = None
    no_fix_handled_correctly: bool | None = None
    if truth.fix_required:
        fix_correct = (
            diagnosis_correct
            and state["status"] == "ready_for_review"
            and bool(state["diff"])
            and _model_paths_only(changed_paths)
            and eligible
            and reconciles
            and no_collateral is True
        )
    else:
        no_fix_handled_correctly = (
            diagnosis_correct
            and str(state["status"]) == contract.outcome
            and not patch_present
            and comparison is None
        )
    return CaseResult(
        scenario=truth.scenario,
        seed=truth.seed,
        expected_cause=truth.cause,
        predicted_cause=predicted,
        status=state["status"],
        diagnosis_correct=diagnosis_correct,
        fix_required=truth.fix_required,
        fix_correct=fix_correct,
        no_fix_handled_correctly=no_fix_handled_correctly,
        no_collateral_damage=no_collateral,
        evidence_quality=evidence_quality,
        false_positive=contract.outcome == "verified_clean"
        and (patch_present or predicted not in {None, truth.cause}),
        steps=state["steps"],
        tokens=state["tokens"],
        clean_net_usd=str(truth.clean_baseline.net_revenue_usd),
        after_net_usd=after_net,
        reasons=list(state["reasons"]),
    )

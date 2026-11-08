"""Local evaluation persists each result before moving to the next case."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_data_reliability.evaluation import local
from ai_data_reliability.evaluation.report import CaseResult
from ai_data_reliability.investigator.local_contract import BASE_DIGEST
from ai_data_reliability.investigator.probes import PROBES
from ai_data_reliability.investigator.provider import OllamaGuidedProvider


def _result(scenario: str, seed: int) -> CaseResult:
    return CaseResult(
        scenario=scenario,
        seed=seed,
        expected_cause="none",
        predicted_cause=None,
        status="failed",
        diagnosis_correct=False,
        fix_required=False,
        fix_correct=None,
        no_fix_handled_correctly=False,
        no_collateral_damage=None,
        evidence_quality=False,
        false_positive=False,
        steps=1,
        tokens=10,
        clean_net_usd="0",
        after_net_usd=None,
        reasons=["No verified conclusion"],
        wall_seconds=1.0,
        failure_category="agent reasoning failure",
        trace={"events": []},
    )


def test_local_evaluation_saves_and_resumes_each_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "_installed_models", lambda: [])
    monkeypatch.setattr(local, "_variant_digest", lambda _models: "variant-digest")
    run = Mock(
        side_effect=lambda _root, scenario, seed, baseline=False, guided=False: _result(
            scenario, seed
        )
    )
    monkeypatch.setattr(local, "_run_case", run)
    first = local.evaluate_local(
        tmp_path, ["clean", "late_refunds"], [7], "agent-version", "fixture-version"
    )
    assert run.call_count == 2
    assert first.provider == "ollama-local" and first.scripted is False
    assert first.model_digest == BASE_DIGEST
    assert first.variant_digest == "variant-digest"
    cases = tmp_path / "cases" / "agent-version-fixture-version-variant-dige"
    assert sorted(path.name for path in cases.glob("*.json")) == [
        "clean-7.json",
        "late_refunds-7.json",
    ]
    second = local.evaluate_local(
        tmp_path, ["clean", "late_refunds"], [7], "agent-version", "fixture-version"
    )
    assert run.call_count == 2
    assert second.results == first.results


def test_timeout_has_its_own_failure_category() -> None:
    from ai_data_reliability.evaluation.scoring import CleanBaseline, GroundTruth

    truth = GroundTruth(
        scenario="clean",
        seed=1,
        cause="none",
        fix_required=False,
        clean_baseline=CleanBaseline(net_revenue_usd=Decimal("0"), order_count=0),
    )
    result = local._error_result(truth, "timeout", "timeout", "Run exceeded 180 seconds", None)
    assert result.status == "timeout"
    assert result.failure_category == "timeout"
    assert result.no_fix_handled_correctly is False


def test_random_baseline_only_requests_distinct_probes() -> None:
    provider = local.RandomProbeProvider()
    evidence: list[dict[str, object]] = []
    for _ in range(6):
        decision = provider.decide(evidence, "select 1")
        assert decision.action == "probe"
        assert decision.probe is not None
        assert decision.cause is None and decision.new_code is None
        evidence.append({"probe": decision.probe})
    assert len({item["probe"] for item in evidence}) == 6
    assert local.RandomProbeProvider().decide([], "select 1").probe == evidence[0]["probe"]


def test_guided_sweep_does_not_retry_an_unrelated_failed_probe() -> None:
    provider = OllamaGuidedProvider()
    evidence: list[dict[str, object]] = [
        {
            "probe": probe,
            "rows": [],
            "error": "query failed" if probe == "payment_join_multiplication" else None,
        }
        for probe in PROBES
    ]
    evidence[2]["rows"] = [["gross_cents"]]
    chat = Mock(
        side_effect=[
            ({"cause": "rename_payment_amount"}, 20),
            ({"new_code": "select 1"}, 20),
        ]
    )
    provider._chat = chat  # type: ignore[method-assign]
    model_code = '{"models/staging/stg_payments.sql":"select 1","models/orders.sql":"select 1"}'
    decision = provider.decide(evidence, model_code)
    assert decision.action == "patch"
    assert decision.support_probe == "payment_amount_schema"
    assert decision.competing_probe == "duplicate_payment_ids"
    assert chat.call_count == 2


def test_guided_no_fix_uses_recorded_evidence_and_no_patch() -> None:
    provider = OllamaGuidedProvider()
    evidence: list[dict[str, object]] = [
        {"probe": probe, "rows": [], "error": None} for probe in PROBES
    ]
    evidence[3]["rows"] = [["refund-1"]]
    chat = Mock(return_value=({"cause": "delay_refund_ingestion"}, 20))
    provider._chat = chat  # type: ignore[method-assign]
    decision = provider.decide(evidence, "{}")
    assert decision.action == "conclude"
    assert decision.outcome == "verified_no_fix"
    assert decision.new_code is None
    assert chat.call_count == 1

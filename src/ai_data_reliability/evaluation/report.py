"""Validated evaluation reports shared by the CLI and read-only API."""

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class CaseResult(BaseModel):
    scenario: str
    seed: int
    expected_cause: str | None
    predicted_cause: str | None
    status: str
    diagnosis_correct: bool
    fix_required: bool = True
    fix_correct: bool | None
    no_fix_handled_correctly: bool | None = None
    no_collateral_damage: bool | None
    evidence_quality: bool
    false_positive: bool
    steps: int
    tokens: int
    cost_usd: None = None
    clean_net_usd: str | None
    after_net_usd: str | None
    reasons: list[str]
    wall_seconds: float | None = None
    failure_category: str | None = None
    trace: dict[str, object] | None = None


class Summary(BaseModel):
    total: int
    diagnosis_correct: int
    fix_correct: int
    fix_required_cases: int = 0
    no_fix_required_cases: int = 0
    no_fix_handled_correctly: int = 0
    evidence_quality: int
    no_collateral_damage: int
    collateral_assessed: int
    false_positives: int
    control_cases: int
    steps: int
    tokens: int
    cost_usd: None = None


class EvaluationReport(BaseModel):
    id: uuid.UUID
    created_at: datetime
    provider: str
    scripted: bool = False
    model: str | None = None
    model_digest: str | None = None
    variant: str | None = None
    variant_digest: str | None = None
    patch_model: str | None = None
    patch_model_digest: str | None = None
    label: str | None = None
    agent_version: str
    fixture_version: str
    contract_version: int = 1
    seeds: list[int] = Field(min_length=1)
    scenarios: list[str] = Field(min_length=1)
    complete_suite: bool
    results: list[CaseResult]
    summary: Summary

    @model_validator(mode="after")
    def label_scripted_provider(self) -> "EvaluationReport":
        self.scripted = self.provider == "fake"
        return self


def summarize(results: list[CaseResult]) -> Summary:
    assessed = [item for item in results if item.no_collateral_damage is not None]
    return Summary(
        total=len(results),
        diagnosis_correct=sum(item.diagnosis_correct for item in results),
        fix_correct=sum(item.fix_correct is True for item in results),
        fix_required_cases=sum(item.fix_required for item in results),
        no_fix_required_cases=sum(not item.fix_required for item in results),
        no_fix_handled_correctly=sum(item.no_fix_handled_correctly is True for item in results),
        evidence_quality=sum(item.evidence_quality for item in results),
        no_collateral_damage=sum(item.no_collateral_damage is True for item in assessed),
        collateral_assessed=len(assessed),
        false_positives=sum(item.false_positive for item in results if item.scenario == "clean"),
        control_cases=sum(item.scenario == "clean" for item in results),
        steps=sum(item.steps for item in results),
        tokens=sum(item.tokens for item in results),
    )


def load_reports(root: Path) -> list[EvaluationReport]:
    reports: list[EvaluationReport] = []
    for path in (root / "reports").glob("*.json"):
        try:
            identifier = uuid.UUID(path.stem)
        except ValueError:
            continue
        if path.stem != str(identifier):
            continue
        reports.append(EvaluationReport.model_validate_json(path.read_text(encoding="utf-8")))
    return sorted(reports, key=lambda report: report.created_at, reverse=True)[:20]

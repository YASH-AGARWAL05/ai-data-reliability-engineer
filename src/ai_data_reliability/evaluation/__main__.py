"""Run the seeded evaluation matrix and save an inspectable report."""

import argparse
import hashlib
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ai_data_reliability.analytics.scenarios import ROOT, SCENARIOS, TEMPLATE, generate
from ai_data_reliability.evaluation.local import evaluate_local
from ai_data_reliability.evaluation.report import CaseResult, EvaluationReport, summarize
from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import Investigator
from ai_data_reliability.investigator.provider import DecisionProvider, FakeProvider, OpenAIProvider
from ai_data_reliability.investigator.sandbox import InvestigatorTools
from ai_data_reliability.service.config import Settings

ALL_SCENARIOS = tuple(sorted(path.stem for path in SCENARIOS.glob("*.json")))


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _agent_version() -> str:
    base = ROOT / "src" / "ai_data_reliability" / "investigator"
    return _fingerprint(
        [base / name for name in ("graph.py", "provider.py", "sandbox.py", "local_policy_v2.py")]
        + [ROOT / "src" / "ai_data_reliability" / "evaluation" / "local.py"]
    )


def _fixture_version() -> str:
    paths = [ROOT / "src" / "ai_data_reliability" / "analytics" / "scenarios.py"]
    paths.extend(path for path in (ROOT / "analytics" / "template").rglob("*") if path.is_file())
    paths.extend(SCENARIOS.glob("*.json"))
    return _fingerprint(paths)


def _run_case(root: Path, scenario: str, seed: int, provider: DecisionProvider) -> CaseResult:
    with tempfile.TemporaryDirectory(dir=root / "work") as directory:
        workspace = Path(directory)
        sources = workspace / "sources"
        sources.mkdir()
        generated = None
        try:
            generated = generate(scenario, seed, sources / "incident")
            tools = InvestigatorTools(sources, workspace / "sandboxes")
            investigator = Investigator(tools, provider, workspace / "checkpoints.sqlite")
            run_id = investigator.start(generated.project)
            state = investigator.run(run_id)
            truth = GroundTruth.model_validate_json(
                generated.ground_truth.read_text(encoding="utf-8")
            )
            return score_case(truth, state)
        except Exception as exc:
            error_truth = (
                GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
                if generated is not None
                else None
            )
            return CaseResult(
                scenario=scenario,
                seed=seed,
                expected_cause=error_truth.cause if error_truth else None,
                predicted_cause=None,
                status="evaluation_error",
                diagnosis_correct=False,
                fix_required=error_truth.fix_required if error_truth else True,
                fix_correct=False if error_truth is None or error_truth.fix_required else None,
                no_fix_handled_correctly=(
                    False if error_truth is not None and not error_truth.fix_required else None
                ),
                no_collateral_damage=None,
                evidence_quality=False,
                false_positive=False,
                steps=0,
                tokens=0,
                clean_net_usd=(
                    str(error_truth.clean_baseline.net_revenue_usd) if error_truth else None
                ),
                after_net_usd=None,
                reasons=[f"Evaluation error: {type(exc).__name__}"],
            )


def evaluate(
    root: Path,
    scenarios: list[str],
    seeds: list[int],
    *,
    provider_name: str = "fake",
    model: str | None = None,
) -> EvaluationReport:
    if (
        not scenarios
        or len(set(scenarios)) != len(scenarios)
        or set(scenarios) - set(ALL_SCENARIOS)
    ):
        raise ValueError("Scenarios must be unique and known")
    if not seeds or len(set(seeds)) != len(seeds) or any(seed < 0 for seed in seeds):
        raise ValueError("Seeds must be unique nonnegative integers")
    if provider_name not in {"fake", "openai", "ollama-local", "ollama-guided", "random-probe"} or (
        provider_name == "openai" and not model
    ):
        raise ValueError("Real evaluation requires an OpenAI model")
    if provider_name in {"ollama-local", "ollama-guided", "random-probe"}:
        return evaluate_local(
            root,
            scenarios,
            seeds,
            _agent_version(),
            _fixture_version(),
            baseline=provider_name == "random-probe",
            guided=provider_name == "ollama-guided",
        )
    root.mkdir(parents=True, exist_ok=True)
    (root / "work").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    results: list[CaseResult] = []
    for scenario in scenarios:
        for seed in seeds:
            provider: DecisionProvider = (
                FakeProvider(scenario, TEMPLATE)
                if provider_name == "fake"
                else OpenAIProvider(model or "")
            )
            item = _run_case(root, scenario, seed, provider)
            results.append(item)
            print(
                f"{scenario} seed={seed}: {item.status}, "
                f"diagnosis={item.diagnosis_correct}, fix={item.fix_correct}",
                flush=True,
            )
    report = EvaluationReport(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        provider=provider_name,
        scripted=provider_name == "fake",
        model=model,
        agent_version=_agent_version(),
        fixture_version=_fixture_version(),
        contract_version=2,
        seeds=seeds,
        scenarios=scenarios,
        complete_suite=set(scenarios) == set(ALL_SCENARIOS) and len(seeds) >= 2,
        results=results,
        summary=summarize(results),
    )
    destination = root / "reports" / f"{report.id}.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the investigator against seeded truth")
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 8, 9])
    parser.add_argument(
        "--scenarios", choices=ALL_SCENARIOS, nargs="+", default=list(ALL_SCENARIOS)
    )
    parser.add_argument(
        "--output-root", type=Path, default=Settings.from_env().run_root.parent / "evaluations"
    )
    parser.add_argument(
        "--provider",
        choices=["fake", "openai", "ollama-local", "ollama-guided", "random-probe"],
        default="fake",
    )
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    arguments = parser.parse_args()
    report = evaluate(
        arguments.output_root,
        arguments.scenarios,
        arguments.seeds,
        provider_name=arguments.provider,
        model=arguments.model if arguments.provider == "openai" else None,
    )
    print(
        f"Saved {report.id}: {report.summary.fix_correct}/{report.summary.total} correct fixes, "
        f"{report.summary.diagnosis_correct}/{report.summary.total} correct diagnoses"
    )


if __name__ == "__main__":
    main()

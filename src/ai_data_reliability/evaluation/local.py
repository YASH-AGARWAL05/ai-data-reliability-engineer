"""Sequential, resumable evaluation using only a downloaded loopback model."""

import json
import random
import signal
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.evaluation.report import CaseResult, EvaluationReport, summarize
from ai_data_reliability.evaluation.scoring import GroundTruth, score_case
from ai_data_reliability.investigator.graph import InvestigationState, Investigator
from ai_data_reliability.investigator.local_contract import (
    BASE_DIGEST,
    BASE_MODEL,
    MAX_STEPS,
    MAX_TOKENS,
    MODEL,
    WALL_SECONDS,
)
from ai_data_reliability.investigator.probes import PROBES
from ai_data_reliability.investigator.provider import (
    Decision,
    OllamaGuidedProvider,
    OllamaLocalProvider,
)
from ai_data_reliability.investigator.sandbox import InvestigatorTools, ToolError

RUN_STEPS = 6
RUN_WALL_SECONDS = 180
VARIANT_DIGEST = "09f0f406acec35dad84ae92016194f6d11a3c0baebec62f1f1d73094b2af35d5"


class CaseTimeout(Exception):
    pass


class RandomProbeProvider:
    """Fixed-seed random queries without a repair or diagnosis policy."""

    def __init__(self) -> None:
        self.random = random.Random(7)
        self.decisions: list[dict[str, object]] = []

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        seen = {item["probe"] for item in evidence}
        remaining = [probe for probe in PROBES if probe not in seen]
        decision = (
            Decision("probe", probe=self.random.choice(remaining))
            if remaining
            else Decision("finish")
        )
        self.decisions.append(
            {"decision": {"action": decision.action, "probe": decision.probe}, "tokens": 0}
        )
        return decision


def _alarm(_signum: int, _frame: object) -> None:
    raise CaseTimeout("Run exceeded 180 seconds")


def _installed_models() -> list[dict[str, object]]:
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get("http://127.0.0.1:11434/api/tags")
        response.raise_for_status()
    models = response.json()["models"]
    if not isinstance(models, list):
        raise ValueError("Local model catalog is invalid")
    return models


def _variant_digest(models: list[dict[str, object]]) -> str:
    variants = [item for item in models if item["name"] == f"{MODEL}:latest"]
    bases = [item for item in models if item["name"] == BASE_MODEL]
    if (
        len(variants) != 1
        or len(bases) != 1
        or bases[0]["digest"] != BASE_DIGEST
        or variants[0]["digest"] != VARIANT_DIGEST
    ):
        raise ValueError("Installed local model differs from frozen contract")
    return str(variants[0]["digest"])


def _patch_model_digest(models: list[dict[str, object]]) -> str:
    matching = [item for item in models if item.get("name") == OllamaGuidedProvider.patch_model]
    if len(matching) != 1 or not isinstance(matching[0].get("digest"), str):
        raise ValueError("Guided patch model is not installed")
    return str(matching[0]["digest"])


def _error_result(
    truth: GroundTruth,
    status: str,
    category: str,
    reason: str,
    state: InvestigationState | None,
) -> CaseResult:
    return CaseResult(
        scenario=truth.scenario,
        seed=truth.seed,
        expected_cause=truth.cause,
        predicted_cause=None,
        status=status,
        diagnosis_correct=False,
        fix_required=truth.fix_required,
        fix_correct=False if truth.fix_required else None,
        no_fix_handled_correctly=False if not truth.fix_required else None,
        no_collateral_damage=None,
        evidence_quality=False,
        false_positive=False,
        steps=int(state["steps"]) if state else 0,
        tokens=int(state["tokens"]) if state else 0,
        clean_net_usd=str(truth.clean_baseline.net_revenue_usd),
        after_net_usd=None,
        reasons=[reason],
        failure_category=category,
    )


def _run_case(
    root: Path, scenario: str, seed: int, *, baseline: bool = False, guided: bool = False
) -> CaseResult:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(dir=root / "work") as directory:
        workspace = Path(directory)
        sources = workspace / "sources"
        sources.mkdir()
        generated = generate(scenario, seed, sources / "incident")
        truth = GroundTruth.model_validate_json(generated.ground_truth.read_text(encoding="utf-8"))
        provider = (
            RandomProbeProvider()
            if baseline
            else OllamaGuidedProvider()
            if guided
            else OllamaLocalProvider()
        )
        tools = InvestigatorTools(sources, workspace / "sandboxes")
        investigator = Investigator(tools, provider, workspace / "checkpoints.sqlite")
        run_id: str | None = None
        state: InvestigationState | None = None
        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, RUN_WALL_SECONDS)
        try:
            run_id = investigator.start(
                generated.project,
                max_steps=MAX_STEPS if guided else RUN_STEPS,
                max_tokens=MAX_TOKENS,
            )
            state = investigator.run(run_id)
            result = score_case(truth, state)
            if not result.diagnosis_correct or (
                result.fix_correct is False or result.no_fix_handled_correctly is False
            ):
                result.failure_category = "agent reasoning"
        except CaseTimeout as exc:
            state = investigator.state(run_id) if run_id is not None else None
            result = _error_result(truth, "timeout", "timeout", str(exc), state)
        except httpx.TimeoutException as exc:
            state = investigator.state(run_id) if run_id is not None else None
            result = _error_result(truth, "timeout", "timeout", type(exc).__name__, state)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            state = investigator.state(run_id) if run_id is not None else None
            result = _error_result(
                truth, "provider_error", "parser or provider failure", type(exc).__name__, state
            )
        except ToolError as exc:
            state = investigator.state(run_id) if run_id is not None else None
            result = _error_result(truth, "tool_error", "tool or sandbox failure", str(exc), state)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        result.wall_seconds = round(time.monotonic() - started, 3)
        result.trace = {
            "decisions": provider.decisions,
            "evidence": state.get("evidence", []) if state else [],
            "events": state.get("events", []) if state else [],
            "reasons": state.get("reasons", []) if state else [],
            "conclusion": state.get("conclusion") if state else None,
            "comparison": state.get("comparison") if state else None,
            "diff": state.get("diff", "") if state else "",
        }
        return result


def evaluate_local(
    root: Path,
    scenarios: list[str],
    seeds: list[int],
    agent_version: str,
    fixture_version: str,
    *,
    baseline: bool = False,
    guided: bool = False,
) -> EvaluationReport:
    if RUN_STEPS > MAX_STEPS or RUN_WALL_SECONDS > WALL_SECONDS:
        raise ValueError("Run limits exceed frozen contract")
    models = [] if baseline else _installed_models()
    digest = None if baseline else _variant_digest(models)
    patch_digest = _patch_model_digest(models) if guided else None
    work = root / "work"
    case_label = (
        f"{agent_version}-{fixture_version}-{digest[:12]}"
        f"{'-' + patch_digest[:12] if patch_digest else ''}"
        if digest
        else f"random-{agent_version}-{fixture_version}"
    )
    cases = root / "cases" / case_label
    reports = root / "reports"
    for path in (work, cases, reports):
        path.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    for scenario in scenarios:
        for seed in seeds:
            destination = cases / f"{scenario}-{seed}.json"
            if destination.exists():
                result = CaseResult.model_validate_json(destination.read_text(encoding="utf-8"))
                print(f"{scenario} seed={seed}: resumed {result.status}", flush=True)
            else:
                result = _run_case(root, scenario, seed, baseline=baseline, guided=guided)
                temporary = destination.with_suffix(".tmp")
                temporary.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
                temporary.replace(destination)
                print(
                    f"{scenario} seed={seed}: {result.status}, {result.wall_seconds}s", flush=True
                )
            results.append(result)
    report = EvaluationReport(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        provider="random-probe" if baseline else "ollama-guided" if guided else "ollama-local",
        scripted=False,
        model=None if baseline else BASE_MODEL,
        model_digest=None if baseline else BASE_DIGEST,
        variant=None if baseline else MODEL,
        variant_digest=digest,
        patch_model=OllamaGuidedProvider.patch_model if guided else None,
        patch_model_digest=patch_digest,
        label=(
            "random-probe baseline, 6 runs, single seed"
            if baseline and len(results) == 6 and seeds == [7]
            else (
                "ollama-guided, llama3.2:3b diagnosis, qwen2.5-coder:7b patch, "
                "fixed probe sweep, single seed"
            )
            if guided
            else (
                "ollama-local, llama3.2:3b, 6 runs, single seed, "
                "structured output constrained to the probe menu"
            )
            if not baseline and len(results) == 6 and seeds == [7]
            else "pilot, plumbing check"
            if not baseline and scenarios == ["clean"] and seeds == [1]
            else None
        ),
        agent_version=agent_version,
        fixture_version=fixture_version,
        contract_version=2,
        seeds=seeds,
        scenarios=scenarios,
        complete_suite=False,
        results=results,
        summary=summarize(results),
    )
    destination = reports / f"{report.id}.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return report

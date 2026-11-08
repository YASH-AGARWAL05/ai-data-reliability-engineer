"""Checkpointed, bounded investigator over the Phase 2 tool boundary."""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ai_data_reliability.investigator.local_contract import QUERY_ROWS
from ai_data_reliability.investigator.probes import PLANS, PROBES, ProbePlan
from ai_data_reliability.investigator.provider import DecisionProvider
from ai_data_reliability.investigator.sandbox import (
    InvestigatorTools,
    MetricComparison,
    MetricSnapshot,
    ToolError,
)

Status = Literal[
    "running",
    "paused",
    "cancelled",
    "ready_for_review",
    "verified_no_fix",
    "verified_clean",
    "failed",
    "exhausted",
]


class InvestigationState(TypedDict):
    run_id: str
    source_project: str
    status: Status
    steps: int
    tokens: int
    max_steps: int
    max_tokens: int
    before_id: str
    after_id: str
    evidence: list[dict[str, object]]
    events: list[dict[str, object]]
    hypotheses: dict[str, dict[str, object]]
    diff: str
    reasons: list[str]
    comparison: dict[str, object] | None
    conclusion: dict[str, object] | None
    changed_paths: list[str]
    baseline_tests_passed: bool


def _linked_rows(
    state: InvestigationState, probe: str, status: str
) -> tuple[int, list[object]] | None:
    hypothesis = state["hypotheses"].get(probe, {})
    evidence_id = hypothesis.get("evidence_id")
    if hypothesis.get("status") != status or not isinstance(evidence_id, int):
        return None
    if not 0 <= evidence_id < len(state["evidence"]):
        return None
    item = state["evidence"][evidence_id]
    rows = item.get("rows")
    if (
        item.get("probe") != probe
        or item.get("run_id") != state["run_id"]
        or item.get("sandbox_id") != state["before_id"]
        or item.get("sql") != PROBES[probe]
        or item.get("error") is not None
        or not isinstance(rows, list)
    ):
        return None
    return evidence_id, rows


def _evidence_pair(
    state: InvestigationState, support_probe: str, contract: ProbePlan
) -> tuple[int, int] | None:
    support = _linked_rows(
        state, support_probe, "supported" if contract.support_has_rows else "refuted"
    )
    competitor = _linked_rows(state, contract.competing_probe, "refuted")
    if support is None or competitor is None:
        return None
    if bool(support[1]) != contract.support_has_rows or competitor[1]:
        return None
    if support[0] == competitor[0]:
        return None
    return support[0], competitor[0]


def _metric_snapshot(snapshot: MetricSnapshot) -> dict[str, object]:
    return {
        "expected_net_usd": str(snapshot.expected_net_usd),
        "actual_net_usd": str(snapshot.actual_net_usd)
        if snapshot.actual_net_usd is not None
        else None,
        "order_count": snapshot.order_count,
        "source_order_count": snapshot.source_order_count,
        "model_rows": [list(row) for row in snapshot.model_rows],
        "pending_refunds": snapshot.pending_refunds,
    }


def _comparison(comparison: MetricComparison) -> dict[str, object]:
    return {
        "before": _metric_snapshot(comparison.before),
        "after": _metric_snapshot(comparison.after),
        "eligible_for_review": comparison.eligible_for_review,
        "reasons": list(comparison.reasons),
    }


class Investigator:
    def __init__(
        self, tools: InvestigatorTools, provider: DecisionProvider, checkpoint_path: Path
    ) -> None:
        self.tools = tools
        self.provider = provider
        self.checkpoint_path = checkpoint_path
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    def _graph(self, saver: SqliteSaver) -> Any:
        graph = StateGraph(InvestigationState)
        graph.add_node("step", self._step)
        graph.add_edge(START, "step")
        graph.add_conditional_edges(
            "step", lambda state: END if state["status"] != "running" else "step"
        )
        return graph.compile(checkpointer=saver, interrupt_after=["step"])

    @contextmanager
    def _saver(self) -> Iterator[SqliteSaver]:
        connection = sqlite3.connect(self.checkpoint_path, check_same_thread=False)
        try:
            yield SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        finally:
            connection.close()

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": str(uuid.UUID(run_id))}}

    def start(
        self,
        source_project: Path,
        *,
        max_steps: int = 10,
        max_tokens: int = 1000,
        run_id: str | None = None,
    ) -> str:
        if max_steps < 1 or max_tokens < 1:
            raise ValueError("Budgets must be positive")
        run_id = str(uuid.uuid4()) if run_id is None else str(uuid.UUID(run_id))
        try:
            existing = self.state(run_id)
        except KeyError:
            pass
        else:
            if existing["source_project"] != str(source_project.resolve(strict=True)):
                raise ValueError("Run identifier belongs to another project")
            return run_id
        initial: InvestigationState = {
            "run_id": run_id,
            "source_project": str(source_project.resolve(strict=True)),
            "status": "running",
            "steps": 0,
            "tokens": 0,
            "max_steps": max_steps,
            "max_tokens": max_tokens,
            "before_id": "",
            "after_id": "",
            "evidence": [],
            "events": [],
            "hypotheses": {},
            "diff": "",
            "reasons": [],
            "comparison": None,
            "conclusion": None,
            "changed_paths": [],
            "baseline_tests_passed": False,
        }
        with self._saver() as saver:
            self._graph(saver).invoke(initial, self._config(run_id))
        return run_id

    def state(self, run_id: str) -> InvestigationState:
        with self._saver() as saver:
            snapshot = self._graph(saver).get_state(self._config(run_id))
        if not snapshot.values:
            raise KeyError(run_id)
        return cast(InvestigationState, snapshot.values)

    def advance(self, run_id: str) -> InvestigationState:
        current = self.state(run_id)
        if current["status"] != "running":
            return current
        with self._saver() as saver:
            self._graph(saver).invoke(None, self._config(run_id))
        return self.state(run_id)

    def run(self, run_id: str) -> InvestigationState:
        current = self.state(run_id)
        while current["status"] == "running":
            current = self.advance(run_id)
        return current

    def pause(self, run_id: str) -> InvestigationState:
        if self.state(run_id)["status"] == "running":
            self._set_status(run_id, "paused")
        return self.state(run_id)

    def resume(self, run_id: str) -> InvestigationState:
        if self.state(run_id)["status"] not in {"paused", "cancelled"}:
            raise ValueError("Only a paused or cancelled investigation can resume")
        self._set_status(run_id, "running")
        return self.state(run_id)

    def cancel(self, run_id: str) -> InvestigationState:
        if self.state(run_id)["status"] in {"running", "paused"}:
            self._set_status(run_id, "cancelled")
        return self.state(run_id)

    def _set_status(self, run_id: str, status: Status) -> None:
        with self._saver() as saver:
            self._graph(saver).update_state(
                self._config(run_id), {"status": status}, as_node="step"
            )

    def _step(self, state: InvestigationState) -> InvestigationState:
        state = {**state, "steps": state["steps"] + 1, "events": state.get("events", [])}

        def record(tool: str, result: str, detail: str = "") -> None:
            state["events"] = [
                *state["events"],
                {"step": state["steps"], "tool": tool, "result": result, "detail": detail},
            ]

        if state["steps"] > state["max_steps"]:
            return {**state, "status": "exhausted", "reasons": ["Step budget exceeded"]}
        if not state["before_id"]:
            try:
                handle = self.tools.create_sandbox(Path(state["source_project"]))
                record("create_sandbox", "completed")
                check = self.tools.run_dbt_tests(handle.sandbox_id)
                record("run_dbt_tests", "passed" if check.tests_passed else "failed", "Baseline")
            except ToolError as exc:
                record("sandbox_or_tests", "failed", str(exc))
                return {**state, "status": "failed", "reasons": [str(exc)]}
            return {
                **state,
                "before_id": handle.sandbox_id,
                "baseline_tests_passed": check.tests_passed,
                "reasons": [] if check.tests_passed else ["Baseline dbt tests failed"],
            }

        try:
            model_code = self.tools.read_model_code(
                state["before_id"], "models/staging/stg_payments.sql"
            )
        except ToolError as exc:
            record("read_model_code", "failed", str(exc))
            return {**state, "status": "failed", "reasons": [str(exc)]}
        record("read_model_code", "completed", "models/staging/stg_payments.sql")
        extra_paths = getattr(self.provider, "extra_model_paths", ())
        if extra_paths:
            model_files = {"models/staging/stg_payments.sql": model_code}
            try:
                for path in extra_paths:
                    model_files[path] = self.tools.read_model_code(state["before_id"], path)
                    record("read_model_code", "completed", path)
            except ToolError as exc:
                record("read_model_code", "failed", str(exc))
                return {**state, "status": "failed", "reasons": [str(exc)]}
            model_code = json.dumps(model_files)
        decision = self.provider.decide(state["evidence"], model_code)
        record("provider_decision", decision.action)
        if decision.tokens < 0:
            return {
                **state,
                "status": "failed",
                "reasons": ["Provider returned negative token usage"],
            }
        state = {**state, "tokens": state["tokens"] + decision.tokens}
        if state["tokens"] > state["max_tokens"]:
            return {**state, "status": "exhausted", "reasons": ["Token budget exceeded"]}
        if decision.action == "probe" and decision.probe in PROBES:
            probe = decision.probe
            attempts = sum(item["probe"] == probe for item in state["evidence"])
            if attempts >= 2:
                return {**state, "status": "failed", "reasons": ["Probe retry limit reached"]}
            item: dict[str, object] = {
                "id": len(state["evidence"]),
                "run_id": state["run_id"],
                "sandbox_id": state["before_id"],
                "probe": probe,
                "sql": PROBES[probe],
            }
            try:
                result = self.tools.run_sql(state["before_id"], PROBES[probe], row_limit=QUERY_ROWS)
                record("run_sql", "completed", probe)
                item.update(
                    {
                        "columns": list(result.columns),
                        "rows": [list(row) for row in result.rows],
                        "error": None,
                    }
                )
                hypotheses = {**state["hypotheses"]}
                hypotheses[probe] = {
                    "status": "supported" if result.rows else "refuted",
                    "evidence_id": item["id"],
                }
                state = {**state, "hypotheses": hypotheses}
            except ToolError as exc:
                record("run_sql", "failed", f"{probe}: {exc}")
                item["error"] = str(exc)
            return {**state, "evidence": [*state["evidence"], item]}
        if decision.action == "patch" and decision.model_path and decision.new_code:
            candidates = (
                [decision.support_probe]
                if decision.support_probe is not None
                else [
                    probe
                    for probe, plan in PLANS.items()
                    if plan.outcome == "patched" and _evidence_pair(state, probe, plan) is not None
                ]
            )
            if len(candidates) != 1 or decision.outcome not in {None, "patched"}:
                return {**state, "status": "failed", "reasons": ["Query evidence is insufficient"]}
            support_probe = candidates[0]
            plan = PLANS.get(support_probe)
            pair = _evidence_pair(state, support_probe, plan) if plan is not None else None
            if (
                plan is None
                or plan.outcome != "patched"
                or pair is None
                or decision.competing_probe not in {None, plan.competing_probe}
            ):
                return {**state, "status": "failed", "reasons": ["Query evidence is insufficient"]}
            cause = decision.cause or support_probe
            try:
                handle = self.tools.create_sandbox(Path(state["source_project"]))
                record("create_sandbox", "completed", "Patch candidate")
                diff = self.tools.apply_patch_in_sandbox(
                    handle.sandbox_id, decision.model_path, decision.new_code
                )
                record("apply_patch_in_sandbox", "completed", decision.model_path)
                check = self.tools.run_dbt_tests(handle.sandbox_id)
                record(
                    "run_dbt_tests", "passed" if check.tests_passed else "failed", "Patch candidate"
                )
                comparison = self.tools.compare_metrics(state["before_id"], handle.sandbox_id)
                record(
                    "compare_metrics",
                    "eligible" if comparison.eligible_for_review else "ineligible",
                )
            except ToolError as exc:
                record("patch_validation", "failed", str(exc))
                return {**state, "status": "failed", "reasons": [str(exc)]}
            if not check.tests_passed or not comparison.eligible_for_review:
                return {
                    **state,
                    "status": "failed",
                    "after_id": handle.sandbox_id,
                    "diff": diff,
                    "reasons": [
                        *([] if check.tests_passed else ["Patch dbt tests failed"]),
                        *comparison.reasons,
                    ],
                    "comparison": _comparison(comparison),
                }
            hypotheses = {
                **state["hypotheses"],
                support_probe: {
                    "status": "verified",
                    "evidence_id": pair[0],
                    "competing_evidence_id": pair[1],
                },
                cause: {
                    "status": "verified",
                    "evidence_id": pair[0],
                    "competing_evidence_id": pair[1],
                },
            }
            return {
                **state,
                "status": "ready_for_review",
                "after_id": handle.sandbox_id,
                "diff": diff,
                "hypotheses": hypotheses,
                "conclusion": {"cause": cause, "outcome": "patched"},
                "changed_paths": [decision.model_path],
                "reasons": [],
                "comparison": _comparison(comparison),
            }
        if decision.action == "conclude" and decision.cause and decision.outcome:
            support_probe = decision.support_probe or ""
            plan = PLANS.get(support_probe)
            pair = _evidence_pair(state, support_probe, plan) if plan is not None else None
            if (
                plan is None
                or plan.outcome != decision.outcome
                or decision.competing_probe not in {None, plan.competing_probe}
                or plan.outcome == "patched"
                or pair is None
                or not state["baseline_tests_passed"]
                or state["diff"]
            ):
                return {**state, "status": "failed", "reasons": ["Query evidence is insufficient"]}
            hypotheses = {
                **state["hypotheses"],
                decision.cause: {
                    "status": "verified",
                    "evidence_id": pair[0],
                    "competing_evidence_id": pair[1],
                },
            }
            return {
                **state,
                "status": cast(Status, plan.outcome),
                "hypotheses": hypotheses,
                "conclusion": {"cause": decision.cause, "outcome": decision.outcome},
                "reasons": [],
            }
        return {
            **state,
            "status": "failed",
            "reasons": ["Provider stopped without a verified patch"],
        }

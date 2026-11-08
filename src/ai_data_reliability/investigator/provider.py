"""Decision providers for the checkpointed investigator."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx
from openai import OpenAI

from ai_data_reliability.investigator.local_contract import (
    MODEL,
    OUTPUT_TOKENS,
    PROMPT,
    SAMPLING_SEED,
    SCHEMA,
)
from ai_data_reliability.investigator.local_policy_v2 import (
    CAUSE_BY_SUPPORT,
    CAUSES,
    MODEL_BY_SUPPORT,
    REPAIR_GUIDANCE_BY_SUPPORT,
)
from ai_data_reliability.investigator.probes import PLANS, PROBES

Probe = str
Action = Literal["probe", "patch", "conclude", "finish"]


@dataclass(frozen=True)
class Decision:
    action: Action
    probe: Probe | None = None
    model_path: str | None = None
    new_code: str | None = None
    tokens: int = 0
    cause: str | None = None
    outcome: str | None = None
    support_probe: str | None = None
    competing_probe: str | None = None


class DecisionProvider(Protocol):
    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision: ...


class FakeProvider:
    """Scenario scripts for exercising the evaluation plumbing."""

    def __init__(self, scenario: str = "duplicate_payments", template: Path | None = None) -> None:
        self.scenario = scenario
        self.template = template

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        scripts = {
            "clean": ("revenue_reconciliation", "pending_refunds", "none", "verified_clean"),
            "duplicate_payments": (
                "duplicate_payment_ids",
                "currency_join_cardinality",
                "duplicate_payment_event",
                "patched",
            ),
            "join_cardinality": (
                "payment_join_multiplication",
                "duplicate_payment_ids",
                "multiply_payment_join",
                "patched",
            ),
            "late_refunds": (
                "pending_refunds",
                "refund_model_error",
                "delay_refund_ingestion",
                "verified_no_fix",
            ),
            "missing_currency_conversion": (
                "missing_fx_conversion",
                "currency_join_cardinality",
                "omit_fx_conversion",
                "patched",
            ),
            "schema_drift": (
                "payment_amount_schema",
                "duplicate_payment_ids",
                "rename_payment_amount",
                "patched",
            ),
        }
        support, competitor, cause, outcome = scripts[self.scenario]
        successful = {str(item["probe"]): item for item in evidence if item.get("error") is None}
        for probe in (support, competitor):
            if probe not in successful:
                return Decision("probe", probe, tokens=20)
        if outcome == "patched":
            patch = (
                model_code.rstrip()
                + "\nqualify row_number() over (partition by p.id order by p.event_id) = 1\n"
            )
            path = "models/staging/stg_payments.sql"
            if self.scenario == "join_cardinality":
                path = "models/orders.sql"
                if self.template is None:
                    raise ValueError("Scripted repair requires a template")
                patch = (self.template / path).read_text(encoding="utf-8")
            elif self.scenario == "missing_currency_conversion":
                if self.template is None:
                    raise ValueError("Scripted repair requires a template")
                patch = (self.template / path).read_text(encoding="utf-8")
            elif self.scenario == "schema_drift":
                if self.template is None:
                    raise ValueError("Scripted repair requires a template")
                patch = (
                    (self.template / path)
                    .read_text(encoding="utf-8")
                    .replace("p.amount / 100.0", "p.gross_cents / 100.0")
                )
            return Decision(
                "patch",
                model_path=path,
                new_code=patch,
                tokens=35,
                cause=cause,
                outcome=outcome,
                support_probe=support,
                competing_probe=competitor,
            )
        return Decision(
            "conclude",
            tokens=10,
            cause=cause,
            outcome=outcome,
            support_probe=support,
            competing_probe=competitor,
        )


class OpenAIProvider:
    """A real Responses API provider; the graph still validates every action."""

    def __init__(self, model: str, client: OpenAI | None = None) -> None:
        self.model = model
        self.client = client or OpenAI(timeout=30.0, max_retries=0)

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        response = self.client.responses.create(
            model=self.model,
            instructions=(
                "Investigate net revenue using only the supplied model and query evidence. "
                "Choose probe, patch, conclude, or finish. Probe names must come from the "
                "supplied list. For patch, provide an existing dbt model SQL path and complete "
                "SQL. For conclude, give a cause and verified_clean or verified_no_fix outcome. "
                "For patch, give a cause and patched outcome. Name the supporting and competing "
                "probes. The graph independently checks query evidence, the competing explanation, "
                "dbt tests, and metric comparison. Never claim verification without those checks."
            ),
            input=json.dumps(
                {"evidence": evidence, "model_code": model_code, "probes": list(PROBES)}
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "investigator_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["probe", "patch", "conclude", "finish"],
                            },
                            "probe": {
                                "type": ["string", "null"],
                                "enum": [*PROBES, None],
                            },
                            "model_path": {"type": ["string", "null"]},
                            "new_code": {"type": ["string", "null"]},
                            "cause": {"type": ["string", "null"]},
                            "outcome": {"type": ["string", "null"]},
                            "support_probe": {"type": ["string", "null"]},
                            "competing_probe": {"type": ["string", "null"]},
                        },
                        "required": [
                            "action",
                            "probe",
                            "model_path",
                            "new_code",
                            "cause",
                            "outcome",
                            "support_probe",
                            "competing_probe",
                        ],
                    },
                }
            },
            max_output_tokens=3000,
            store=False,
        )
        data = json.loads(response.output_text)
        if not isinstance(data, dict) or data.get("action") not in {
            "probe",
            "patch",
            "conclude",
            "finish",
        }:
            raise ValueError("Provider returned an invalid action")
        probe = data.get("probe")
        if probe is not None and probe not in PROBES:
            raise ValueError("Provider returned an invalid probe")
        if response.usage is None:
            raise ValueError("Provider response has no token usage")
        return Decision(
            action=data["action"],
            probe=probe,
            model_path=data.get("model_path"),
            new_code=data.get("new_code"),
            tokens=response.usage.total_tokens,
            cause=data.get("cause"),
            outcome=data.get("outcome"),
            support_probe=data.get("support_probe"),
            competing_probe=data.get("competing_probe"),
        )


class OllamaLocalProvider:
    """Unscripted inference through the loopback-only Ollama API."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, object]] = []

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "model_code": model_code,
                            "evidence": evidence,
                            "probes": list(PROBES),
                        }
                    ),
                },
            ],
            "format": SCHEMA,
            "stream": False,
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "10m"),
            "options": {
                "num_ctx": 8192,
                "temperature": 0,
                "seed": SAMPLING_SEED,
                "num_predict": OUTPUT_TOKENS,
            },
        }
        response = httpx.post(
            "http://127.0.0.1:11434/api/chat", json=body, timeout=90, trust_env=False
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload["message"]["content"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.decisions.append(
                {
                    "source": "provider_parse_error",
                    "raw_content": raw,
                    "eval_count": payload.get("eval_count"),
                    "done_reason": payload.get("done_reason"),
                }
            )
            raise
        if data.get("action") not in {"probe", "patch", "conclude", "finish"}:
            raise ValueError("Local provider returned an invalid action")
        if data.get("probe") is not None and data["probe"] not in PROBES:
            raise ValueError("Local provider returned an invalid probe")
        tokens = int(payload.get("prompt_eval_count", 0)) + int(payload.get("eval_count", 0))
        self.decisions.append({"decision": data, "tokens": tokens})
        return Decision(
            action=data["action"],
            probe=data.get("probe"),
            model_path=data.get("model_path"),
            new_code=data.get("new_code"),
            cause=data.get("cause"),
            outcome=data.get("outcome"),
            support_probe=data.get("support_probe"),
            competing_probe=data.get("competing_probe"),
            tokens=tokens,
        )


class OllamaGuidedProvider(OllamaLocalProvider):
    """Run the fixed diagnostic menu before asking the model to select an outcome."""

    extra_model_paths = ("models/orders.sql",)
    patch_model = "qwen2.5-coder:7b"

    def _chat(
        self,
        instruction: str,
        content: dict[str, object],
        schema: dict[str, object],
        *,
        model: str = MODEL,
        output_tokens: int = OUTPUT_TOKENS,
    ) -> tuple[dict[str, object], int]:
        response = httpx.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(content)},
                ],
                "format": schema,
                "stream": False,
                "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "10m"),
                "options": {
                    "num_ctx": 8192,
                    "temperature": 0,
                    "seed": SAMPLING_SEED,
                    "num_predict": output_tokens,
                },
            },
            timeout=90,
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload["message"]["content"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.decisions.append(
                {
                    "source": "provider_parse_error",
                    "raw_content": raw,
                    "eval_count": payload.get("eval_count"),
                    "done_reason": payload.get("done_reason"),
                }
            )
            raise
        if not isinstance(data, dict):
            raise ValueError("Local provider returned invalid JSON object")
        tokens = int(payload.get("prompt_eval_count", 0)) + int(payload.get("eval_count", 0))
        return data, tokens

    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        seen = {str(item["probe"]) for item in evidence}
        for probe in PROBES:
            if probe not in seen:
                decision = Decision("probe", probe=probe)
                self.decisions.append(
                    {
                        "decision": {"action": "probe", "probe": probe},
                        "tokens": 0,
                        "source": "sweep",
                    }
                )
                return decision
        rows = {str(item["probe"]): item.get("rows") for item in evidence}
        candidates = [
            {
                "cause": CAUSE_BY_SUPPORT[support],
                "support_probe": support,
                "competing_probe": plan.competing_probe,
                "outcome": plan.outcome,
            }
            for support, plan in PLANS.items()
            if isinstance(rows.get(support), list)
            and bool(rows[support]) == plan.support_has_rows
            and rows.get(plan.competing_probe) == []
        ]
        if not candidates:
            return Decision("finish")
        data, tokens = self._chat(
            "Choose the cause best supported by the recorded query results. "
            "Choose only from the evidence-backed candidates. Return its exact cause label.",
            {"evidence": evidence, "candidates": candidates, "cause_descriptions": CAUSES},
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cause": {"type": "string", "enum": [c["cause"] for c in candidates]}
                },
                "required": ["cause"],
            },
        )
        selected = next((item for item in candidates if item["cause"] == data.get("cause")), None)
        self.decisions.append({"decision": data, "tokens": tokens, "source": "model_selection"})
        if selected is None:
            return Decision("finish", tokens=tokens)
        outcome = str(selected["outcome"])
        if outcome != "patched":
            return Decision(
                "conclude",
                tokens=tokens,
                cause=str(selected["cause"]),
                outcome=outcome,
                support_probe=str(selected["support_probe"]),
                competing_probe=str(selected["competing_probe"]),
            )
        path = MODEL_BY_SUPPORT[str(selected["support_probe"])]
        files = json.loads(model_code)
        code, patch_tokens = self._chat(
            "Repair the supplied dbt SQL model for the selected diagnosis. Make the smallest "
            "change that fixes the defect. Preserve model output columns, joins unrelated to "
            "the defect, and all unrelated rows. Do not filter to example IDs in query results. "
            "Do not invent tables or columns. Return complete corrected SQL as new_code, "
            "with actual newline characters.",
            {
                "model_path": path,
                "current_sql": files[path],
                "diagnosis": selected,
                "repair_guidance": REPAIR_GUIDANCE_BY_SUPPORT[str(selected["support_probe"])],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"new_code": {"type": "string"}},
                "required": ["new_code"],
            },
            model=self.patch_model,
            output_tokens=1536,
        )
        self.decisions.append({"decision": code, "tokens": patch_tokens, "source": "model_patch"})
        new_code = code.get("new_code")
        if isinstance(new_code, str) and "\n" not in new_code and "\\n" in new_code:
            new_code = new_code.replace("\\n", "\n")
        return Decision(
            "patch",
            model_path=path,
            new_code=new_code if isinstance(new_code, str) else None,
            tokens=tokens + patch_tokens,
            cause=str(selected["cause"]),
            outcome=outcome,
            support_probe=str(selected["support_probe"]),
            competing_probe=str(selected["competing_probe"]),
        )

"""Frozen local-model decision contract for the single-seed evaluation."""

from ai_data_reliability.investigator.probes import PROBES

MODEL = "dre-agent-3b"
BASE_MODEL = "llama3.2:3b"
BASE_DIGEST = "a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72"
MAX_STEPS = 10
MAX_TOKENS = 20000
WALL_SECONDS = 300
OUTPUT_TOKENS = 768
QUERY_ROWS = 20
SAMPLING_SEED = 7

PROMPT = (
    "Investigate net revenue using only the supplied SQL model and recorded query results. "
    "Choose one action: probe, patch, conclude, or finish. Use only a listed probe name. "
    "Test a plausible competing explanation before proposing a patch or conclusion. "
    "For patch, return the complete SQL for one existing dbt model and name the supporting "
    "and competing probes. For conclude, give a cause and either verified_clean or "
    "verified_no_fix. The graph independently checks evidence, dbt tests, metrics, and "
    "unrelated models. Return only the JSON decision."
)

SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["probe", "patch", "conclude", "finish"]},
        "probe": {"type": ["string", "null"], "enum": [*PROBES, None]},
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
}

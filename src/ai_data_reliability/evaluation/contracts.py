"""Evidence and outcome contract for the seeded ground-truth causes."""

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["patched", "verified_no_fix", "verified_clean"]


@dataclass(frozen=True)
class CauseContract:
    outcome: Outcome
    fix_required: bool
    supporting_probe: str
    competing_probe: str
    support_has_rows: bool


CAUSES = {
    "none": CauseContract(
        "verified_clean", False, "revenue_reconciliation", "pending_refunds", False
    ),
    "duplicate_payment_event": CauseContract(
        "patched", True, "duplicate_payment_ids", "currency_join_cardinality", True
    ),
    "multiply_payment_join": CauseContract(
        "patched", True, "payment_join_multiplication", "duplicate_payment_ids", True
    ),
    "delay_refund_ingestion": CauseContract(
        "verified_no_fix", False, "pending_refunds", "refund_model_error", True
    ),
    "omit_fx_conversion": CauseContract(
        "patched", True, "missing_fx_conversion", "currency_join_cardinality", True
    ),
    "rename_payment_amount": CauseContract(
        "patched", True, "payment_amount_schema", "duplicate_payment_ids", True
    ),
}

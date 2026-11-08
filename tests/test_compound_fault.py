"""A real two-fault warehouse must not validate one isolated diagnosis."""

import csv
from pathlib import Path

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.investigator.graph import Investigator
from ai_data_reliability.investigator.provider import Decision
from ai_data_reliability.investigator.sandbox import InvestigatorTools


class SingleCauseProvider:
    def decide(self, evidence: list[dict[str, object]], model_code: str) -> Decision:
        probes = ("payment_join_multiplication", "duplicate_payment_ids")
        if len(evidence) < len(probes):
            return Decision("probe", probe=probes[len(evidence)], tokens=1)
        return Decision(
            "patch",
            model_path="models/orders.sql",
            new_code="select 1 as order_id",
            tokens=1,
            cause="multiply_payment_join",
            outcome="patched",
            support_probe=probes[0],
            competing_probe=probes[1],
        )


def test_real_compound_fault_refuses_single_cause(tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    generated = generate(
        "join_cardinality",
        7,
        source_root / "incident",
        additional_mutation="duplicate_payment_event",
    )
    with (generated.project / "seeds" / "raw_payments.csv").open(newline="") as stream:
        payments = list(csv.DictReader(stream))
    assert len({row["id"] for row in payments}) < len(payments)
    assert (
        "left join {{ ref('stg_ingestion_events') }}"
        in (generated.project / "models" / "orders.sql").read_text()
    )

    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    investigator = Investigator(tools, SingleCauseProvider(), tmp_path / "checkpoints.sqlite")
    final = investigator.run(investigator.start(generated.project))

    assert final["status"] == "failed"
    assert final["reasons"] == ["Query evidence is insufficient"]
    assert [item["probe"] for item in final["evidence"]] == [
        "payment_join_multiplication",
        "duplicate_payment_ids",
    ]
    assert all(item["error"] is None and item["rows"] for item in final["evidence"])
    assert final["after_id"] == ""
    assert final["diff"] == ""
    assert final["conclusion"] is None

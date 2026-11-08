"""Generate isolated Jaffle Shop incident fixtures from seeded configs."""

import argparse
import csv
import json
import random
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

VALID_MUTATIONS = {
    "none",
    "duplicate_payment_event",
    "multiply_payment_join",
    "omit_fx_conversion",
    "delay_refund_ingestion",
    "rename_payment_amount",
}

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "analytics" / "template"
SCENARIOS = ROOT / "analytics" / "scenarios"
RATES = {"USD": Decimal("1.00"), "EUR": Decimal("1.10"), "GBP": Decimal("1.30")}
PAYMENT_COLUMNS = [
    "id",
    "order_id",
    "payment_method",
    "amount",
    "currency",
    "ingested_at",
    "event_id",
]
REFUND_COLUMNS = ["id", "order_id", "amount_cents", "created_at", "ingested_at"]
EVENT_COLUMNS = ["event_id", "event_type", "order_id", "status", "expected_at", "arrived_at"]


@dataclass(frozen=True)
class GeneratedScenario:
    project: Path
    ground_truth: Path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _scenario_config(name: str) -> dict[str, object]:
    path = SCENARIOS / f"{name}.json"
    if name not in {item.stem for item in SCENARIOS.glob("*.json")}:
        raise ValueError(f"Unknown scenario: {name}")
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != name:
        raise ValueError(f"Invalid scenario config: {path}")
    return data


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def generate(
    name: str, seed: int, output: Path, *, additional_mutation: str | None = None
) -> GeneratedScenario:
    config = _scenario_config(name)
    mutation = config.get("mutation")
    if mutation not in VALID_MUTATIONS:
        raise ValueError(f"Unsupported mutation: {mutation}")
    if additional_mutation is not None and (
        additional_mutation != "duplicate_payment_event" or mutation != "multiply_payment_join"
    ):
        raise ValueError(f"Unsupported additional mutation: {additional_mutation}")
    output = output.resolve()
    if output == TEMPLATE or TEMPLATE in output.parents:
        raise ValueError("Output must be outside the source template")
    output.mkdir(parents=True, exist_ok=False)
    project = output / "project"
    shutil.copytree(TEMPLATE, project)
    seeds = project / "seeds"
    # A stable seed lets the same incident be reproduced across runs.
    rng = random.Random(seed)  # nosec B311

    orders = _read_csv(seeds / "raw_orders.csv")
    original_payments = _read_csv(seeds / "raw_payments.csv")
    order_by_id = {row["id"]: row for row in orders}
    eligible_payments = [row for row in original_payments if int(row["order_id"]) >= 70]

    payments: list[dict[str, str]] = []
    for index, row in enumerate(original_payments):
        currency = (
            ("EUR", "GBP")[index] if index < 2 else rng.choices(list(RATES), weights=[6, 3, 1])[0]
        )
        payments.append(
            {
                **row,
                "currency": currency,
                "ingested_at": order_by_id[row["order_id"]]["order_date"],
                "event_id": f"pay-{row['id']}-1",
            }
        )

    chosen_orders = rng.sample(sorted({row["order_id"] for row in eligible_payments}), 3)
    payment_by_order = {row["order_id"]: row for row in payments}
    refunds = [
        {
            "id": str(index),
            "order_id": order_id,
            "amount_cents": str(int(payment_by_order[order_id]["amount"]) // 4),
            "created_at": order_by_id[order_id]["order_date"],
            "ingested_at": order_by_id[order_id]["order_date"],
        }
        for index, order_id in enumerate(chosen_orders, start=1)
    ]
    clean_gross = sum(
        (Decimal(row["amount"]) * RATES[row["currency"]] / 100 for row in payments),
        Decimal(0),
    )
    clean_refunds = sum((Decimal(row["amount_cents"]) / 100 for row in refunds), Decimal(0))
    events = [
        {
            "event_id": row["event_id"],
            "event_type": "payment",
            "order_id": row["order_id"],
            "status": "arrived",
            "expected_at": row["ingested_at"],
            "arrived_at": row["ingested_at"],
        }
        for row in payments
    ]
    events.extend(
        {
            "event_id": f"refund-{row['id']}",
            "event_type": "refund",
            "order_id": row["order_id"],
            "status": "arrived",
            "expected_at": row["created_at"],
            "arrived_at": row["ingested_at"],
        }
        for row in refunds
    )

    affected_ids: list[str] = []
    if mutation == "duplicate_payment_event" or additional_mutation == "duplicate_payment_event":
        selected = rng.choice([row for row in payments if row["order_id"] in chosen_orders])
        duplicate = {**selected, "event_id": f"pay-{selected['id']}-2"}
        payments.append(duplicate)
        source_event = next(event for event in events if event["event_id"] == selected["event_id"])
        events.append({**source_event, "event_id": duplicate["event_id"]})
        affected_ids = [selected["id"]]
    if mutation == "multiply_payment_join":
        path = project / "models" / "orders.sql"
        source = path.read_text(encoding="utf-8")
        before = (
            "select\n"
            "        order_id,\n"
            "        sum(amount_usd) as gross_amount\n"
            "    from {{ ref('stg_payments') }}\n"
            "    group by order_id"
        )
        after = (
            "select\n"
            "        p.order_id,\n"
            "        sum(p.amount_usd) as gross_amount\n"
            "    from {{ ref('stg_payments') }} as p\n"
            "    left join {{ ref('stg_ingestion_events') }} as i"
            " on p.order_id = i.order_id\n"
            "    group by p.order_id"
        )
        if source.count(before) != 1:
            raise ValueError("Unexpected orders model; join mutation cannot be applied")
        path.write_text(source.replace(before, after), encoding="utf-8")
        affected_ids = chosen_orders
    elif mutation == "omit_fx_conversion":
        path = project / "models" / "staging" / "stg_payments.sql"
        source = path.read_text(encoding="utf-8")
        before = "p.amount / 100.0 * c.usd_rate"
        if source.count(before) != 1:
            raise ValueError("Unexpected payment model; FX mutation cannot be applied")
        path.write_text(source.replace(before, "p.amount / 100.0"), encoding="utf-8")
        affected_ids = [row["id"] for row in payments if row["currency"] != "USD"]
    elif mutation == "delay_refund_ingestion":
        selected = rng.choice(refunds)
        refunds.remove(selected)
        for event in events:
            if event["event_id"] == f"refund-{selected['id']}":
                event["status"] = "pending"
                event["arrived_at"] = ""
        affected_ids = [selected["id"]]
    elif mutation == "rename_payment_amount":
        affected_ids = ["amount"]

    _write_csv(seeds / "raw_payments.csv", PAYMENT_COLUMNS, payments)
    if mutation == "rename_payment_amount":
        for row in payments:
            row["gross_cents"] = row.pop("amount")
        _write_csv(
            seeds / "raw_payments.csv",
            [*PAYMENT_COLUMNS[:3], "gross_cents", *PAYMENT_COLUMNS[4:]],
            payments,
        )
    _write_csv(seeds / "raw_refunds.csv", REFUND_COLUMNS, refunds)
    _write_csv(
        seeds / "raw_currencies.csv",
        ["currency", "usd_rate"],
        [{"currency": currency, "usd_rate": str(rate)} for currency, rate in RATES.items()],
    )
    _write_csv(seeds / "raw_ingestion_events.csv", EVENT_COLUMNS, events)
    _write_csv(
        seeds / "raw_subscriptions.csv",
        ["subscription_id", "customer_id", "plan", "started_at"],
        [
            {
                "subscription_id": f"sub-{row['id']}",
                "customer_id": row["id"],
                "plan": rng.choice(["basic", "plus"]),
                "started_at": "2018-01-01",
            }
            for row in _read_csv(seeds / "raw_customers.csv")
        ],
    )

    truth = output / "ground_truth.json"
    truth.write_text(
        json.dumps(
            {
                "scenario": name,
                "seed": seed,
                "cause": mutation,
                **({"additional_cause": additional_mutation} if additional_mutation else {}),
                "fix_required": config["fix_required"],
                "expected_dbt": config["expected_dbt"],
                "affected_ids": affected_ids,
                "clean_baseline": {
                    "order_count": len(orders),
                    "gross_revenue_usd": _money(clean_gross),
                    "refunds_usd": _money(clean_refunds),
                    "net_revenue_usd": _money(clean_gross - clean_refunds),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return GeneratedScenario(project=project, ground_truth=truth)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an isolated seeded Jaffle Shop scenario")
    parser.add_argument("scenario", choices=sorted(path.stem for path in SCENARIOS.glob("*.json")))
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = generate(args.scenario, args.seed, args.output)
    print(result.project)


if __name__ == "__main__":
    main()

import json
import os
import subprocess
from pathlib import Path

import duckdb
import pytest

from ai_data_reliability.analytics.scenarios import SCENARIOS, TEMPLATE, generate


def test_generation_is_seeded_and_keeps_truth_outside_project(tmp_path: Path) -> None:
    first = generate("duplicate_payments", 7, tmp_path / "first")
    second = generate("duplicate_payments", 7, tmp_path / "second")
    third = generate("duplicate_payments", 8, tmp_path / "third")

    assert first.ground_truth.read_bytes() == second.ground_truth.read_bytes()
    assert (first.project / "seeds" / "raw_payments.csv").read_bytes() == (
        second.project / "seeds" / "raw_payments.csv"
    ).read_bytes()
    assert first.ground_truth not in first.project.rglob("*")
    assert (TEMPLATE / "seeds" / "raw_payments.csv").read_text().splitlines()[0] == (
        "id,order_id,payment_method,amount"
    )
    assert (
        json.loads(first.ground_truth.read_text())["affected_ids"]
        != json.loads(third.ground_truth.read_text())["affected_ids"]
    )


def test_unknown_scenario_and_template_output_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        generate("unknown", 1, tmp_path / "run")
    with pytest.raises(ValueError, match="outside the source template"):
        generate("clean", 1, TEMPLATE / "run")


@pytest.mark.parametrize("scenario", sorted(path.stem for path in SCENARIOS.glob("*.json")))
def test_scenario_dbt_outcome_and_metric(scenario: str, tmp_path: Path) -> None:
    generated = generate(scenario, 7, tmp_path / scenario)
    truth = json.loads(generated.ground_truth.read_text())
    dbt = str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "dbt")
    environment = {**os.environ, "DBT_DUCKDB_PATH": str(generated.project / "warehouse.duckdb")}

    def run_dbt(command: str) -> subprocess.CompletedProcess[str]:
        args = [dbt, command, "--profiles-dir", str(generated.project), "--no-use-colors"]
        if command == "seed":
            args.append("--full-refresh")
        return subprocess.run(
            args,
            cwd=generated.project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    seeded = run_dbt("seed")
    assert seeded.returncode == 0, seeded.stdout[-3000:]
    built = run_dbt("run")
    expected = truth["expected_dbt"]
    if expected == "fail_build":
        assert built.returncode != 0
        run_results = json.loads((generated.project / "target" / "run_results.json").read_text())
        assert any(
            item["status"] == "error" and "stg_payments" in item["unique_id"]
            for item in run_results["results"]
        ), built.stdout[-3000:]
    else:
        assert built.returncode == 0, built.stdout[-3000:]
        tested = run_dbt("test")
        run_results = json.loads((generated.project / "target" / "run_results.json").read_text())
        statuses = {item["status"] for item in run_results["results"]}
        if expected == "pass":
            assert tested.returncode == 0, tested.stdout[-3000:]
            assert statuses <= {"pass"}
        else:
            assert expected == "fail_test"
            assert tested.returncode != 0
            assert any(
                item["status"] == "fail" and "assert_revenue_reconciles" in item["unique_id"]
                for item in run_results["results"]
            ), tested.stdout[-3000:]

    if expected != "fail_build":
        with duckdb.connect(str(generated.project / "warehouse.duckdb"), read_only=True) as db:
            metric_row = db.execute(
                "select sum(order_count), sum(net_revenue_usd) from fct_daily_revenue"
            ).fetchone()
            pending_row = db.execute(
                "select count(*) from stg_ingestion_events "
                "where event_type = 'refund' and status = 'pending'"
            ).fetchone()
        assert metric_row is not None
        assert pending_row is not None
        order_count, net_revenue = metric_row
        pending_refunds = pending_row[0]
        assert order_count == truth["clean_baseline"]["order_count"]
        clean_net = float(truth["clean_baseline"]["net_revenue_usd"])
        if scenario == "clean":
            assert abs(net_revenue - clean_net) < 0.001
            assert pending_refunds == 0
        else:
            assert abs(net_revenue - clean_net) > 0.001
            assert pending_refunds == (1 if scenario == "late_refunds" else 0)

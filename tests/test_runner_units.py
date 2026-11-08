"""Fast Docker runner tests used by mutation testing."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from docker.analytics import runner


def test_runner_query_rejects_external_files_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "warehouse.duckdb"
    mounted_file = tmp_path / "mounted.txt"
    mounted_file.write_text("secret")
    with duckdb.connect(str(database_path)) as database:
        database.execute("create table orders (id integer)")
        database.execute("insert into orders values (1), (2)")
    monkeypatch.setattr(runner, "DB", database_path)
    original_connect = duckdb.connect
    read_only_values: list[object] = []

    def watched_connect(*args: Any, **kwargs: Any) -> duckdb.DuckDBPyConnection:
        read_only_values.append(kwargs.get("read_only"))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", watched_connect)

    def execute(sql: str, limit: int = 100) -> dict[str, Any]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(sql))
        runner.query(limit)
        result: dict[str, Any] = json.loads(capsys.readouterr().out)
        return result

    result = execute("select id from orders order by id", 1)
    assert result == {"columns": ["id"], "rows": [[1]], "truncated": True}
    assert read_only_values == [True]
    assert execute("select current_setting('enable_external_access')")["rows"] == [[False]]
    for sql in (
        f"select * from read_text('{mounted_file}')",
        f"select * from read_csv('{mounted_file}')",
        f"select * from glob('{tmp_path}/*')",
    ):
        with pytest.raises(duckdb.Error):
            execute(sql)
    for sql in ("drop table orders", "select 1; delete from orders"):
        with pytest.raises(ValueError):
            execute(sql)
    for limit in (0, 1001):
        with pytest.raises(ValueError):
            execute("select 1", limit)
    assert execute("select count(*) from orders")["rows"] == [[2]]


def test_runner_hashes_all_models_by_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "aa_missing.sql").write_text("select 1")
    (models / "customers.sql").write_text("select 1")
    (models / "subscriptions.sql").write_text("select 1")
    monkeypatch.setattr(runner, "MODELS", models)
    with duckdb.connect(":memory:") as database:
        database.execute("create table customers (customer_id integer, plan varchar)")
        database.execute("insert into customers values (1, 'basic'), (2, 'plus')")
        before = runner.model_row_hashes(database)
        assert set(before) == {"aa_missing", "customers", "subscriptions"}
        assert before["aa_missing"] is None
        assert before["subscriptions"] is None
        database.execute("update customers set plan = 'premium' where customer_id = 1")
        after = runner.model_row_hashes(database)
        assert before["customers"] != after["customers"]
        assert database.execute("select count(*), sum(customer_id) from customers").fetchone() == (
            2,
            3,
        )
    (models / "invalid-name.sql").write_text("select 1")
    with duckdb.connect(":memory:") as database, pytest.raises(ValueError):
        runner.model_row_hashes(database)


def test_runner_build_stops_at_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 1 if command[1] == "run" else 0, stdout="output", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.build()
    result = json.loads(capsys.readouterr().out)
    assert [command[1] for command in commands] == ["seed", "run"]
    assert "--full-refresh" in commands[0]
    assert "--full-refresh" not in commands[1]
    assert all("/state/target" in command and "/state/logs" in command for command in commands)
    assert all("/workspace" in command and "--no-use-colors" in command for command in commands)
    assert not result["tests_passed"]


def test_runner_build_requires_all_three_steps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.build()
    result = json.loads(capsys.readouterr().out)
    assert [step["command"] for step in result["steps"]] == ["seed", "run", "test"]
    assert [command[1] for command in commands] == ["seed", "run", "test"]
    assert result["tests_passed"]


def test_runner_metrics_reports_each_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "warehouse.duckdb"
    models = tmp_path / "models"
    models.mkdir()
    (models / "subscriptions.sql").write_text("select 1")
    with duckdb.connect(str(database_path)) as database:
        database.execute(
            "create table raw_payments as select 1 as id, 1 as order_id, "
            "200 as amount, 'USD' as currency, '2020-01-01' as ingested_at, 'e1' as event_id"
        )
        database.execute("create table raw_currencies as select 'USD' as currency, 1.0 as usd_rate")
        database.execute("create table raw_refunds (amount_cents integer)")
        database.execute("create table raw_orders as select 1 as id")
        database.execute("create table raw_ingestion_events (event_type varchar, status varchar)")
        database.execute(
            "create table fct_daily_revenue as select 1 as order_count, 2.0 as net_revenue_usd"
        )
        database.execute("create table subscriptions as select 1 as customer_id, 'basic' as plan")
    monkeypatch.setattr(runner, "DB", database_path)
    monkeypatch.setattr(runner, "MODELS", models)
    runner.metrics()
    result = json.loads(capsys.readouterr().out)
    assert result["expected_net_usd"] == 2.0
    assert float(result["actual_net_usd"]) == 2.0
    assert result["source_order_count"] == 1
    assert len(result["model_rows"]["subscriptions"]) == 64


def test_runner_main_dispatches_only_supported_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runner, "query", lambda limit: calls.append(f"query:{limit}"))
    monkeypatch.setattr(runner, "build", lambda: calls.append("build"))
    monkeypatch.setattr(runner, "metrics", lambda: calls.append("metrics"))
    for arguments in (["runner", "query", "5"], ["runner", "build"], ["runner", "metrics"]):
        monkeypatch.setattr(sys, "argv", arguments)
        runner.main()
    assert calls == ["query:5", "build", "metrics"]
    monkeypatch.setattr(sys, "argv", ["runner", "delete"])
    with pytest.raises(ValueError):
        runner.main()

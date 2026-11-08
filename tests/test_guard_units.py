"""Fast boundary tests used by mutation testing without starting Docker."""

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.investigator import sandbox
from ai_data_reliability.investigator.sandbox import (
    InvestigatorTools,
    MetricSnapshot,
    SQLResult,
    ToolError,
)


def fixture(tmp_path: Path, scenario: str = "clean") -> tuple[InvestigatorTools, str]:
    root = tmp_path / "runs"
    source = generate(scenario, 7, root / scenario)
    tools = InvestigatorTools(root, tmp_path / "sandboxes")
    return tools, tools.create_sandbox(source.project).sandbox_id


def test_source_root_and_copy_are_confined(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    source = generate("clean", 7, root / "clean")
    with pytest.raises(ValueError):
        InvestigatorTools(root, root)
    with pytest.raises(ValueError):
        InvestigatorTools(root, root / "nested")
    tools = InvestigatorTools(root, tmp_path / "sandboxes")
    outside = generate("clean", 8, tmp_path / "other")
    with pytest.raises(ToolError):
        tools.create_sandbox(outside.project)
    (source.project / "warehouse.duckdb").write_bytes(b"do not copy")
    (source.project / "target").mkdir()
    (source.project / "target" / "compiled.sql").write_text("do not copy")
    handle = tools.create_sandbox(source.project)
    assert handle.project.parent.parent == tools.sandbox_root
    assert (handle.project / "models" / "orders.sql").is_file()
    assert not (handle.project / "ground_truth.json").exists()
    assert not (handle.project.parent / "ground_truth.json").exists()
    assert not (handle.project / "warehouse.duckdb").exists()
    assert not (handle.project / "target").exists()
    assert (handle.project.parent / "guard" / "protected.json").is_file()
    (source.project / "ground_truth.json").write_text("hidden")
    with pytest.raises(ToolError, match="Ground truth"):
        tools.create_sandbox(source.project)


def test_protected_manifest_detects_add_remove_edit_and_symlink(tmp_path: Path) -> None:
    tools, identifier = fixture(tmp_path)
    project = tools._paths(identifier)[0]
    manifest = sandbox._protected_manifest(project)
    assert "tests/assert_revenue_reconciles.sql" in manifest
    assert "seeds/raw_orders.csv" in manifest
    test = project / "tests" / "assert_revenue_reconciles.sql"
    original = test.read_text()
    test.write_text("select 1")
    with pytest.raises(ToolError):
        tools._check_protected(identifier)
    test.write_text(original)
    addition = project / "tests" / "new.sql"
    addition.write_text("select 1")
    with pytest.raises(ToolError):
        tools._check_protected(identifier)
    addition.unlink()
    test.unlink()
    with pytest.raises(ToolError):
        tools._check_protected(identifier)
    test.write_text(original)
    link = project / "models" / "leak.sql"
    link.symlink_to(tmp_path / "outside")
    with pytest.raises(ToolError):
        sandbox._protected_manifest(project)


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_protected_manifest_covers_added_model_configuration(tmp_path: Path, suffix: str) -> None:
    tools, identifier = fixture(tmp_path)
    project = tools._paths(identifier)[0]
    configuration = project / "models" / f"unexpected{suffix}"
    configuration.write_text("version: 2\n")
    with pytest.raises(ToolError, match="Protected"):
        tools._check_protected(identifier)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.sql",
        "/tmp/outside.sql",
        "models/../../tests/assert_revenue_reconciles.sql",
        "tests/assert_revenue_reconciles.sql",
        "models/schema.yml",
        "models/missing.sql",
    ],
)
def test_model_path_rejects_non_models(tmp_path: Path, path: str) -> None:
    tools, identifier = fixture(tmp_path)
    with pytest.raises(ToolError):
        tools.read_model_code(identifier, path)


def test_model_path_rejects_symlink_and_invalid_sandbox_id(tmp_path: Path) -> None:
    tools, identifier = fixture(tmp_path)
    with pytest.raises(ToolError):
        tools.read_model_code("../escape", "models/orders.sql")
    target = tmp_path / "outside.sql"
    target.write_text("outside")
    path = tools._paths(identifier)[0] / "models" / "orders.sql"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ToolError):
        tools.read_model_code(identifier, "models/orders.sql")


def test_model_path_rejects_symlink_to_another_model(tmp_path: Path) -> None:
    tools, identifier = fixture(tmp_path)
    project = tools._paths(identifier)[0]
    path = project / "models" / "orders.sql"
    path.unlink()
    path.symlink_to(project / "models" / "staging" / "stg_payments.sql")
    with pytest.raises(ToolError, match="escapes the sandbox"):
        tools.read_model_code(identifier, "models/orders.sql")


@pytest.mark.parametrize("directory", ["project", "state", "guard"])
def test_sandbox_directory_swap_is_rejected(tmp_path: Path, directory: str) -> None:
    tools, identifier = fixture(tmp_path)
    path = tools.sandbox_root / identifier / directory
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    path.rename(tmp_path / "old")
    path.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(ToolError, match="replaced"):
        tools._paths(identifier)


@pytest.mark.parametrize(
    "code",
    [
        "{{ adapter.get_relation() }}\nselect 1",
        "{% do log('x') %}\nselect 1",
        "{{ load_file('/etc/passwd') }}\nselect 1",
        "{{ env_var('HOME') }}\nselect 1",
        "{{ modules.os.environ }}\nselect 1",
        "{% call statement('x') %}select 1{% endcall %}",
        "select * from {{ ref(variable) }}",
        "select * from {{ ref('orders') | upper }}",
        "select * from {{ source('a') }}",
        "select * from {{ ref('a') }}; delete from orders",
        "delete from orders",
        "select * from {{ ref('orders')",
        "select * from ref('orders') }}",
        "select 1 {# hidden #}",
    ],
)
def test_model_allowlist_rejects_expressions_and_statements(code: str) -> None:
    with pytest.raises(ToolError):
        sandbox._validate_model_sql(code)


def test_model_allowlist_accepts_only_literal_relation_calls() -> None:
    sandbox._validate_model_sql(
        "with chosen as (select * from {{ ref('stg_orders') }}) "
        'select * from chosen union all select * from {{ source("commerce", "orders") }}'
    )


def test_patch_changes_only_model_and_rejects_noop(tmp_path: Path) -> None:
    tools, identifier = fixture(tmp_path)
    path = "models/orders.sql"
    original = tools.read_model_code(identifier, path)
    new_code = original.replace("coalesce(p.gross_amount, 0)", "coalesce(p.gross_amount, 0.0)")
    diff = tools.apply_patch_in_sandbox(identifier, path, new_code)
    assert "+    coalesce(p.gross_amount, 0.0)" in diff
    assert tools.read_model_code(identifier, path) == new_code
    with pytest.raises(ToolError):
        tools.apply_patch_in_sandbox(identifier, path, new_code)
    with pytest.raises(ToolError):
        tools.apply_patch_in_sandbox(identifier, "tests/assert_revenue_reconciles.sql", "select 1")
    with pytest.raises(ToolError):
        tools.apply_patch_in_sandbox(identifier, path, "select '" + "x" * 100_001 + "'")
    test = tools._paths(identifier)[0] / "tests" / "assert_revenue_reconciles.sql"
    test.write_text("select 1")
    with pytest.raises(ToolError, match="Protected"):
        tools.apply_patch_in_sandbox(identifier, path, "select 1")


def test_container_mounts_are_separated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, identifier = fixture(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tools._container(identifier, "query", stdin="select 1") == {"ok": True}
    query = commands[-1]
    assert query[query.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert query[query.index("--network") + 1] == "none"
    assert "--read-only" in query
    assert "--cap-drop" in query and "ALL" in query
    assert "--security-opt" in query and "no-new-privileges" in query
    assert "--rm" in query and "--pull=never" in query
    assert query[query.index("--memory") + 1] == "1g"
    assert query[query.index("--cpus") + 1] == "1"
    assert query[query.index("--pids-limit") + 1] == "64"
    assert query[query.index("--tmpfs") + 1] == "/tmp:rw,nosuid,nodev,size=64m"
    assert "-i" in query and query[query.index("-i") - 1] == "run"
    assert any("target=/workspace,readonly" in part for part in query)
    assert any("target=/state,readonly" in part for part in query)
    assert query[query.index("--env") + 1] == "HOME=/tmp"
    assert "DBT_DUCKDB_PATH=/state/warehouse.duckdb" in query
    assert tools._container(identifier, "build", writable_state=True) == {"ok": True}
    build = commands[-1]
    assert any("target=/state" in part and "readonly" not in part for part in build)
    assert any("target=/workspace,readonly" in part for part in build)


def test_container_timeout_kills_named_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, identifier = fixture(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "run" in command:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(sandbox.ToolTimeout):
        tools._container(identifier, "query", timeout_seconds=1)
    assert commands[1] == ["docker", "kill", commands[0][commands[0].index("--name") + 1]]


def test_run_sql_limits_and_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, identifier = fixture(tmp_path)
    calls: list[tuple[str, tuple[str, ...], str | None, int]] = []

    def fake_container(
        _identifier: str,
        operation: str,
        arguments: tuple[str, ...] = (),
        stdin: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        calls.append((operation, arguments, stdin, timeout_seconds))
        return {"columns": ["id"], "rows": [[1]], "truncated": False}

    monkeypatch.setattr(
        tools,
        "_container",
        fake_container,
    )
    result = tools.run_sql(identifier, "select 1", row_limit=1)
    assert result.columns == ("id",) and result.rows == ((1,),) and not result.truncated
    assert calls == [("query", ("1",), "select 1", 10)]
    for limit in (0, 1001):
        with pytest.raises(ToolError):
            tools.run_sql(identifier, "select 1", row_limit=limit)
    for timeout in (0, 31):
        with pytest.raises(ToolError):
            tools.run_sql(identifier, "select 1", timeout_seconds=timeout)
    with pytest.raises(ToolError):
        tools.run_sql(identifier, "x" * 20_001)
    assert tools.run_sql(identifier, "select 1", row_limit=1000, timeout_seconds=30).rows == ((1,),)
    assert calls[-1] == ("query", ("1000",), "select 1", 30)


def test_read_tools_confine_lineage_changes_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, identifier = fixture(tmp_path, "join_cardinality")
    lineage = tools.get_lineage(identifier, "models/orders.sql")
    assert "stg_ingestion_events" in lineage.upstream
    assert "fct_daily_revenue" in lineage.downstream
    changes = tools.get_recent_changes(identifier, "models/orders.sql")
    assert "stg_ingestion_events" in changes.template_diff

    queries: list[str] = []

    def fake_sql(_identifier: str, sql: str, row_limit: int = 100) -> SQLResult:
        queries.append(sql)
        if "information_schema.tables" in sql:
            assert row_limit == 1000
            return SQLResult(("table_name",), (("orders",),), False)
        return SQLResult(
            ("column_name", "data_type", "is_nullable"),
            (("order_id", "INTEGER", "NO"),),
            False,
        )

    monkeypatch.setattr(tools, "run_sql", fake_sql)
    assert tools.list_tables(identifier) == ("orders",)
    assert tools.describe_table(identifier, "orders")[0].name == "order_id"
    assert "table_name = 'orders'" in queries[-1]
    with pytest.raises(ToolError):
        tools.describe_table(identifier, "orders'; delete from orders")


def test_affected_models_excludes_every_downstream_model(tmp_path: Path) -> None:
    tools, identifier = fixture(tmp_path)
    project = tools._paths(identifier)[0]
    assert sandbox._affected_models(project, {"stg_payments"}) == {
        "stg_payments",
        "customers",
        "orders",
        "fct_daily_revenue",
    }
    assert "subscriptions" not in sandbox._affected_models(project, {"stg_payments"})
    assert "subscriptions" not in sandbox._revenue_lineage(project)
    assert "stg_payments" in sandbox._revenue_lineage(project)


def test_dbt_result_and_snapshot_are_bound_to_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, identifier = fixture(tmp_path)
    project, state, guard = tools._paths(identifier)
    (state / "warehouse.duckdb").write_bytes(b"warehouse")

    def fake_container(_identifier: str, operation: str, **_: Any) -> dict[str, Any]:
        if operation == "build":
            return {
                "steps": [
                    {"command": name, "returncode": 0, "output": "ok"}
                    for name in ("seed", "run", "test")
                ],
                "tests_passed": True,
            }
        return {
            "expected_net_usd": "100.00",
            "actual_net_usd": "100.00",
            "actual_order_count": 99,
            "source_order_count": 99,
            "model_rows": {"customers": "abc", "subscriptions": "def"},
            "pending_refunds": 0,
        }

    monkeypatch.setattr(tools, "_container", fake_container)
    check = tools.run_dbt_tests(identifier)
    assert check.tests_passed and [step.command for step in check.steps] == ["seed", "run", "test"]
    record = json.loads((guard / "last_test.json").read_text())
    assert record["model_digest"] == sandbox._model_digest(project)
    assert record["database_digest"] == sandbox._sha256(state / "warehouse.duckdb")
    snapshot = tools._snapshot(identifier)
    assert snapshot.actual_net_usd == Decimal("100.00")
    assert dict(snapshot.model_rows) == {"customers": "abc", "subscriptions": "def"}


def test_metric_guard_checks_all_unrelated_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    source = generate("duplicate_payments", 7, root / "duplicate")
    tools = InvestigatorTools(root, tmp_path / "sandboxes")
    before = tools.create_sandbox(source.project)
    after = tools.create_sandbox(source.project)
    path = "models/staging/stg_payments.sql"
    code = tools.read_model_code(after.sandbox_id, path)
    tools.apply_patch_in_sandbox(
        after.sandbox_id, path, code.replace("p.amount /", "p.amount * 1 /")
    )
    after_project, state, guard = tools._paths(after.sandbox_id)
    (state / "warehouse.duckdb").write_bytes(b"state")
    (guard / "last_test.json").write_text(
        json.dumps(
            {
                "model_digest": sandbox._model_digest(after_project),
                "database_digest": sandbox._sha256(state / "warehouse.duckdb"),
                "tests_passed": True,
            }
        )
    )

    def snapshot(actual: str, rows: dict[str, str], pending: int = 0) -> MetricSnapshot:
        return MetricSnapshot(
            expected_net_usd=Decimal("100"),
            actual_net_usd=Decimal(actual),
            order_count=99,
            source_order_count=99,
            model_rows=tuple(sorted(rows.items())),
            pending_refunds=pending,
        )

    before_snapshot = snapshot("110", {"stg_customers": "one", "subscriptions": "one"})
    after_snapshot = snapshot("100", {"stg_customers": "one", "subscriptions": "one"})
    monkeypatch.setattr(
        tools,
        "_snapshot",
        lambda identifier: before_snapshot if identifier == before.sandbox_id else after_snapshot,
    )
    assert tools.compare_metrics(before.sandbox_id, after.sandbox_id).eligible_for_review
    after_snapshot = snapshot("100", {"stg_customers": "one", "subscriptions": "two"})
    assert (
        "Unrelated models changed: subscriptions"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    after_snapshot = snapshot("100", {"stg_customers": "one"})
    assert (
        "Unrelated models changed: subscriptions"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    after_snapshot = snapshot("100", {"stg_customers": "one", "subscriptions": "one"})
    before_snapshot = snapshot("100", {"stg_customers": "one", "subscriptions": "one"})
    assert (
        "No pre-existing metric discrepancy"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    before_snapshot = snapshot("110", {"stg_customers": "one", "subscriptions": "one"})
    after_snapshot = snapshot("101", {"stg_customers": "one", "subscriptions": "one"})
    assert (
        "Net revenue does not reconcile to raw events"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    after_snapshot = snapshot("100", {"stg_customers": "one", "subscriptions": "one"}, 1)
    assert (
        "Refund ingestion is incomplete"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    after_snapshot = MetricSnapshot(
        expected_net_usd=Decimal("100"),
        actual_net_usd=Decimal("100"),
        order_count=98,
        source_order_count=99,
        model_rows=after_snapshot.model_rows,
        pending_refunds=0,
    )
    assert (
        "Order count changed" in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    before_snapshot = MetricSnapshot(
        expected_net_usd=Decimal("100"),
        actual_net_usd=Decimal("110"),
        order_count=98,
        source_order_count=99,
        model_rows=before_snapshot.model_rows,
        pending_refunds=0,
    )
    assert (
        "Order count changed" in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    subscription_path = "models/subscriptions.sql"
    subscription_code = tools.read_model_code(after.sandbox_id, subscription_path)
    tools.apply_patch_in_sandbox(
        after.sandbox_id, subscription_path, subscription_code + "\nwhere true\n"
    )
    (guard / "last_test.json").write_text(
        json.dumps(
            {
                "model_digest": sandbox._model_digest(after_project),
                "database_digest": sandbox._sha256(state / "warehouse.duckdb"),
                "tests_passed": True,
            }
        )
    )
    assert (
        "Unrelated models patched: subscriptions"
        in tools.compare_metrics(before.sandbox_id, after.sandbox_id).reasons
    )
    (guard / "last_test.json").write_text('{"tests_passed": false}')
    rejected = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert "dbt tests did not pass" in rejected.reasons
    assert "Model changed after dbt tests" in rejected.reasons
    assert "Warehouse changed after dbt tests" in rejected.reasons

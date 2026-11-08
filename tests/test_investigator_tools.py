import subprocess
import time
import uuid
from pathlib import Path

import duckdb
import pytest

from ai_data_reliability.analytics.scenarios import generate
from ai_data_reliability.investigator.sandbox import InvestigatorTools, ToolError, ToolTimeout


def _fixture(tmp_path: Path, scenario: str = "clean") -> tuple[InvestigatorTools, str, Path]:
    source_root = tmp_path / "runs"
    source = generate(scenario, 7, source_root / scenario)
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    handle = tools.create_sandbox(source.project)
    return tools, handle.sandbox_id, source.project


def test_copy_excludes_truth_and_does_not_change_source(tmp_path: Path) -> None:
    tools, sandbox_id, source = _fixture(tmp_path)
    project = tools._paths(sandbox_id)[0]
    assert project != source
    assert not list(project.rglob("ground_truth.json"))
    assert not (project / "warehouse.duckdb").exists()
    assert (source.parent / "ground_truth.json").is_file()
    source_code = (source / "models" / "orders.sql").read_bytes()
    patch = tools.read_model_code(sandbox_id, "models/orders.sql").replace(
        "coalesce(p.gross_amount, 0) as gross_amount",
        "coalesce(p.gross_amount, 0.0) as gross_amount",
    )
    diff = tools.apply_patch_in_sandbox(sandbox_id, "models/orders.sql", patch)
    assert "+    coalesce(p.gross_amount, 0.0)" in diff
    assert (source / "models" / "orders.sql").read_bytes() == source_code


@pytest.mark.parametrize(
    "path",
    [
        "../ground_truth.json",
        "/tmp/outside.sql",
        "models/../../tests/assert_revenue_reconciles.sql",
        "tests/assert_revenue_reconciles.sql",
        "models/schema.yml",
        "models/unknown.sql",
    ],
)
def test_patch_rejects_non_models_and_traversal(tmp_path: Path, path: str) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    with pytest.raises(ToolError):
        tools.apply_patch_in_sandbox(sandbox_id, path, "select 1\n")


@pytest.mark.parametrize(
    "expression",
    [
        "{{ config(enabled=false) }}",
        "{{ adapter.get_relation(database='main', schema='main', identifier='orders') }}",
        "{% do log('escaped') %}",
        "{{ load_file('/workspace/dbt_project.yml') }}",
        "{{ env_var('HOME') }}",
        "{{ modules.os.environ }}",
        "{% call statement('escape') %}select 1{% endcall %}",
        "{{ ref('orders') | upper }}",
        "{{ ref(variable_name) }}",
    ],
)
def test_patch_rejects_unapproved_jinja(tmp_path: Path, expression: str) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    original = tools.read_model_code(sandbox_id, "models/orders.sql")
    with pytest.raises(ToolError, match="Only"):
        tools.apply_patch_in_sandbox(sandbox_id, "models/orders.sql", expression + "\nselect 1\n")
    assert tools.read_model_code(sandbox_id, "models/orders.sql") == original


def test_patch_accepts_literal_ref_and_source_only(tmp_path: Path) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    diff = tools.apply_patch_in_sandbox(
        sandbox_id,
        "models/orders.sql",
        "select * from {{ ref('stg_orders') }}\nunion all\n"
        "select * from {{ source('commerce', 'orders') }}\n",
    )
    assert "+select * from {{ source('commerce', 'orders') }}" in diff


def test_patch_rejects_symlink_escape(tmp_path: Path) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    outside = tmp_path / "outside.sql"
    outside.write_text("keep me")
    model = tools._paths(sandbox_id)[0] / "models" / "orders.sql"
    model.unlink()
    model.symlink_to(outside)
    with pytest.raises(ToolError, match="Symlinks"):
        tools.apply_patch_in_sandbox(sandbox_id, "models/orders.sql", "select 2\n")
    assert outside.read_text() == "keep me"


def test_source_symlink_and_truth_in_project_are_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    source = generate("clean", 7, source_root / "clean")
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    (source.project / "seeds" / "external.csv").symlink_to(source.ground_truth)
    with pytest.raises(ToolError, match="Symlinks"):
        tools.create_sandbox(source.project)
    (source.project / "seeds" / "external.csv").unlink()
    (source.project / "ground_truth.json").write_text("hidden")
    with pytest.raises(ToolError, match="Ground truth"):
        tools.create_sandbox(source.project)


def test_source_outside_allowlist_and_sandbox_id_escape_are_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    source_root.mkdir()
    outside = generate("clean", 7, tmp_path / "outside")
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    with pytest.raises(ToolError, match="allowed source root"):
        tools.create_sandbox(outside.project)
    with pytest.raises(ToolError, match="Invalid sandbox"):
        tools.read_model_code("../outside", "models/orders.sql")


def test_protected_test_and_seed_changes_are_detected(tmp_path: Path) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    project = tools._paths(sandbox_id)[0]
    test_file = project / "tests" / "assert_revenue_reconciles.sql"
    test_file.write_text("select 1 where false")
    with pytest.raises(ToolError, match="Protected"):
        tools.run_dbt_tests(sandbox_id)


def test_read_tools_show_lineage_and_fault_diff(tmp_path: Path) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path, "join_cardinality")
    lineage = tools.get_lineage(sandbox_id, "models/orders.sql")
    assert "stg_ingestion_events" in lineage.upstream
    assert "fct_daily_revenue" in lineage.downstream
    changes = tools.get_recent_changes(sandbox_id, "models/orders.sql")
    assert "+    left join {{ ref('stg_ingestion_events') }}" in changes.template_diff
    assert changes.commits


def test_read_only_sql_limits_and_schema_tools(tmp_path: Path) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    assert tools.run_dbt_tests(sandbox_id).tests_passed
    result = tools.run_sql(sandbox_id, "select order_id from orders order by order_id", 2)
    assert result.rows == ((1,), (2,))
    assert result.truncated
    assert "orders" in tools.list_tables(sandbox_id)
    assert any(column.name == "order_id" for column in tools.describe_table(sandbox_id, "orders"))
    with pytest.raises(ToolError):
        tools.describe_table(sandbox_id, "orders'; drop table orders; --")
    for sql in (
        "drop table orders",
        "copy (select * from orders) to '/state/leak.csv'",
        "select 1; delete from orders",
    ):
        with pytest.raises(ToolError, match="Only one SELECT"):
            tools.run_sql(sandbox_id, sql)
    assert not (tools._paths(sandbox_id)[1] / "leak.csv").exists()
    with pytest.raises(ToolError):
        tools.run_sql(sandbox_id, "select * from read_text('/workspace/ground_truth.json')")
    assert tools.run_sql(sandbox_id, "select current_setting('enable_external_access')").rows == (
        (False,),
    )
    for sql in (
        "select * from read_text('/workspace/dbt_project.yml')",
        "select * from read_csv('/workspace/seeds/raw_customers.csv')",
        "select * from glob('/workspace/seeds/*.csv')",
    ):
        with pytest.raises(ToolError):
            tools.run_sql(sandbox_id, sql)
    with pytest.raises(ToolError):
        tools.run_sql(sandbox_id, "select 1", row_limit=1001)
    with pytest.raises(ToolError):
        tools.run_sql(sandbox_id, "select from orders")
    assert tools.run_sql(sandbox_id, "select count(*) from orders").rows == ((99,),)


def test_query_timeout_stops_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, sandbox_id, _ = _fixture(tmp_path)
    assert tools.run_dbt_tests(sandbox_id).tests_passed
    container_id = uuid.uuid4()
    monkeypatch.setattr(uuid, "uuid4", lambda: container_id)
    with pytest.raises(ToolTimeout):
        tools.run_sql(
            sandbox_id,
            "select sum(i) from range(1000000000000) as t(i)",
            timeout_seconds=1,
        )
    deadline = time.monotonic() + 5
    while True:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name=^/air-{container_id.hex}$",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if not result.stdout.strip():
            break
        assert time.monotonic() < deadline, "Timed-out container is still running"
        time.sleep(0.1)


def test_metric_comparison_requires_reconciliation_and_regression(tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    source = generate("duplicate_payments", 7, source_root / "duplicate")
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    before = tools.create_sandbox(source.project)
    after = tools.create_sandbox(source.project)
    assert not tools.run_dbt_tests(before.sandbox_id).tests_passed
    assert not tools.run_dbt_tests(after.sandbox_id).tests_passed
    unpatched = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert not unpatched.eligible_for_review
    assert "dbt tests did not pass" in unpatched.reasons

    code = tools.read_model_code(after.sandbox_id, "models/staging/stg_payments.sql")
    deduplicated = code.replace(
        "from {{ ref('raw_payments') }} as p",
        "from (\n"
        "    select * exclude (event_rank) from (\n"
        "        select *, row_number() over (\n"
        "            partition by id order by ingested_at desc, event_id desc\n"
        "        ) as event_rank\n"
        "        from {{ ref('raw_payments') }}\n"
        "    ) where event_rank = 1\n"
        ") as p",
    )
    tools.apply_patch_in_sandbox(after.sandbox_id, "models/staging/stg_payments.sql", deduplicated)
    assert tools.run_dbt_tests(after.sandbox_id).tests_passed
    comparison = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert comparison.eligible_for_review, comparison.reasons
    assert comparison.before.actual_net_usd is not None
    assert comparison.after.actual_net_usd is not None
    assert comparison.before.actual_net_usd > comparison.after.actual_net_usd
    assert comparison.after.actual_net_usd == comparison.after.expected_net_usd
    assert (
        dict(comparison.before.model_rows)["stg_customers"]
        == dict(comparison.after.model_rows)["stg_customers"]
    )

    no_improvement = tools.create_sandbox(source.project)
    tools.apply_patch_in_sandbox(
        no_improvement.sandbox_id, "models/staging/stg_payments.sql", deduplicated
    )
    status_only = tools.read_model_code(no_improvement.sandbox_id, "models/orders.sql").replace(
        "coalesce(p.gross_amount, 0) as gross_amount",
        "coalesce(p.gross_amount, 0.0) as gross_amount",
    )
    tools.apply_patch_in_sandbox(no_improvement.sandbox_id, "models/orders.sql", status_only)
    assert tools.run_dbt_tests(no_improvement.sandbox_id).tests_passed
    green_only = tools.compare_metrics(after.sandbox_id, no_improvement.sandbox_id)
    assert not green_only.eligible_for_review
    assert "No pre-existing metric discrepancy" in green_only.reasons

    before_aggregate = tools.run_sql(
        after.sandbox_id, "select count(*), sum(customer_id) from subscriptions"
    ).rows
    with duckdb.connect(str(tools._paths(after.sandbox_id)[1] / "warehouse.duckdb")) as db:
        db.execute(
            "update subscriptions set plan = case customer_id "
            "when 1 then 'changed-a' when 2 then 'changed-b' else plan end"
        )
    assert (
        tools.run_sql(after.sandbox_id, "select count(*), sum(customer_id) from subscriptions").rows
        == before_aggregate
    )
    row_level_change = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert "Unrelated models changed: subscriptions" in row_level_change.reasons

    with duckdb.connect(str(tools._paths(after.sandbox_id)[1] / "warehouse.duckdb")) as db:
        db.execute("update fct_daily_revenue set net_revenue_usd = net_revenue_usd + 1")
    changed_after_test = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert "Warehouse changed after dbt tests" in changed_after_test.reasons

    protected = after.project / "tests" / "assert_revenue_reconciles.sql"
    protected.write_text("select 1 where false")
    with pytest.raises(ToolError, match="Protected"):
        tools.compare_metrics(before.sandbox_id, after.sandbox_id)


def test_schema_drift_fix_can_be_reviewed_without_editing_tests(tmp_path: Path) -> None:
    source_root = tmp_path / "runs"
    source = generate("schema_drift", 7, source_root / "drift")
    tools = InvestigatorTools(source_root, tmp_path / "sandboxes")
    before = tools.create_sandbox(source.project)
    after = tools.create_sandbox(source.project)
    assert not tools.run_dbt_tests(before.sandbox_id).tests_passed
    code = tools.read_model_code(after.sandbox_id, "models/staging/stg_payments.sql")
    assert "p.amount /" in code
    tools.apply_patch_in_sandbox(
        after.sandbox_id,
        "models/staging/stg_payments.sql",
        code.replace("p.amount /", "p.gross_cents /"),
    )
    assert tools.run_dbt_tests(after.sandbox_id).tests_passed
    comparison = tools.compare_metrics(before.sandbox_id, after.sandbox_id)
    assert comparison.eligible_for_review, comparison.reasons
    assert comparison.before.actual_net_usd is None
    assert comparison.after.actual_net_usd == comparison.after.expected_net_usd

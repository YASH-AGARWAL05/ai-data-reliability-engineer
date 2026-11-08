"""Commands executed inside a container with only one sandbox mounted."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb

DB = Path("/state/warehouse.duckdb")
MODELS = Path("/workspace/models")
MODEL_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def model_row_hashes(database: duckdb.DuckDBPyConnection) -> dict[str, str | None]:
    existing = {
        row[0]
        for row in database.execute(
            "select table_name from information_schema.tables where table_schema = 'main'"
        ).fetchall()
    }
    fingerprints: dict[str, str | None] = {}
    for path in sorted(MODELS.rglob("*.sql")):
        name = path.stem
        if not MODEL_NAME.fullmatch(name) or name in fingerprints:
            raise ValueError("Invalid or duplicate model name")
        if name not in existing:
            fingerprints[name] = None
            continue
        digest = hashlib.sha256()
        cursor = database.execute(f'select * from "{name}" order by all')  # nosec B608
        while rows := cursor.fetchmany(1000):
            for row in rows:
                payload = json.dumps(row, default=str, separators=(",", ":")).encode("utf-8")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
        fingerprints[name] = digest.hexdigest()
    return fingerprints


def query(row_limit: int) -> None:
    sql = sys.stdin.read().strip()
    statements = duckdb.extract_statements(sql)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Only one SELECT statement is permitted")
    if not 1 <= row_limit <= 1000:
        raise ValueError("Row limit must be between 1 and 1000")
    with duckdb.connect(str(DB), read_only=True) as database:
        database.execute("set enable_external_access = false")
        # DuckDB parsed a single SELECT and the database mount is read only.
        cursor = database.execute(
            f"select * from ({statements[0].query.rstrip(';')}) "  # nosec B608
            f"as limited_query limit {row_limit + 1}"
        )
        rows = cursor.fetchall()
        print(
            json.dumps(
                {
                    "columns": [column[0] for column in cursor.description],
                    "rows": rows[:row_limit],
                    "truncated": len(rows) > row_limit,
                },
                default=str,
            )
        )


def build() -> None:
    steps: list[dict[str, Any]] = []
    for command in ("seed", "run", "test"):
        args = [
            "dbt",
            command,
            "--project-dir",
            "/workspace",
            "--profiles-dir",
            "/workspace",
            "--target-path",
            "/state/target",
            "--log-path",
            "/state/logs",
            "--no-use-colors",
        ]
        if command == "seed":
            args.append("--full-refresh")
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=100)
        steps.append(
            {
                "command": command,
                "returncode": result.returncode,
                "output": result.stdout[-4000:] + result.stderr[-1000:],
            }
        )
        if result.returncode:
            break
    print(
        json.dumps(
            {"steps": steps, "tests_passed": len(steps) == 3 and steps[-1]["returncode"] == 0}
        )
    )


def metrics() -> None:
    with duckdb.connect(str(DB), read_only=True) as database:
        columns = {
            row[0]
            for row in database.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'main' and table_name = 'raw_payments'"
            ).fetchall()
        }
        amount_column = "amount" if "amount" in columns else "gross_cents"
        if amount_column not in columns:
            raise ValueError("Payment amount column is missing")
        # The column name can only be one of two fixed source-schema variants.
        expected = database.execute(
            f"""
            with latest as (
                select *, row_number() over (
                    partition by id order by ingested_at desc, event_id desc
                ) as event_rank
                from raw_payments
            ), payments as (
                select sum(p.{amount_column} / 100.0 * c.usd_rate) as gross
                from latest as p
                join raw_currencies as c on p.currency = c.currency
                where p.event_rank = 1
            ), refunds as (
                select sum(amount_cents / 100.0) as total from raw_refunds
            )
            select payments.gross - coalesce(refunds.total, 0) from payments, refunds
            """  # nosec B608
        ).fetchone()
        has_revenue = database.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'main' and table_name = 'fct_daily_revenue'"
        ).fetchone()
        actual = (
            database.execute(
                "select sum(order_count), sum(net_revenue_usd) from fct_daily_revenue"
            ).fetchone()
            if has_revenue and has_revenue[0]
            else (None, None)
        )
        source_orders = database.execute("select count(*) from raw_orders").fetchone()
        model_rows = model_row_hashes(database)
        pending = database.execute(
            "select count(*) from raw_ingestion_events "
            "where event_type = 'refund' and status = 'pending'"
        ).fetchone()
    if expected is None or actual is None or source_orders is None or pending is None:
        raise ValueError("Metric query returned no row")
    print(
        json.dumps(
            {
                "expected_net_usd": expected[0],
                "actual_order_count": actual[0],
                "source_order_count": source_orders[0],
                "actual_net_usd": actual[1],
                "model_rows": model_rows,
                "pending_refunds": pending[0],
            },
            default=str,
        )
    )


def main() -> None:
    if len(sys.argv) < 2:
        raise ValueError("Expected a runner command")
    if sys.argv[1] == "query" and len(sys.argv) == 3:
        query(int(sys.argv[2]))
    elif sys.argv[1] == "build" and len(sys.argv) == 2:
        build()
    elif sys.argv[1] == "metrics" and len(sys.argv) == 2:
        metrics()
    else:
        raise ValueError("Unsupported runner command")


if __name__ == "__main__":
    main()

"""Tool boundary for isolated, reviewable warehouse investigations."""

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_MODELS = ROOT / "analytics" / "template" / "models"
IMAGE = "ai-reliability-dbt:phase2"
REF_PATTERN = re.compile(r"\bref\(\s*['\"]([a-z][a-z0-9_]*)['\"]\s*\)")
TABLE_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
REF_CALL = re.compile(r"ref\(\s*(['\"])([a-z][a-z0-9_]*)\1\s*\)\Z")
SOURCE_CALL = re.compile(
    r"source\(\s*(['\"])([a-z][a-z0-9_]*)\1\s*,\s*(['\"])([a-z][a-z0-9_]*)\3\s*\)\Z"
)
JINJA_EXPRESSION = re.compile(r"{{(.*?)}}", re.DOTALL)


class ToolError(Exception):
    """A tool request was rejected or failed without changing trusted inputs."""


class ToolTimeout(ToolError):
    """The container did not finish within its fixed time budget."""


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str
    project: Path


@dataclass(frozen=True)
class SQLResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class Lineage:
    upstream: tuple[str, ...]
    downstream: tuple[str, ...]


@dataclass(frozen=True)
class RecentChanges:
    template_diff: str
    commits: tuple[str, ...]


@dataclass(frozen=True)
class DbtStep:
    command: str
    passed: bool
    output: str


@dataclass(frozen=True)
class DbtCheck:
    steps: tuple[DbtStep, ...]
    tests_passed: bool


@dataclass(frozen=True)
class MetricSnapshot:
    expected_net_usd: Decimal
    actual_net_usd: Decimal | None
    order_count: int | None
    source_order_count: int
    model_rows: tuple[tuple[str, str | None], ...]
    pending_refunds: int


@dataclass(frozen=True)
class MetricComparison:
    before: MetricSnapshot
    after: MetricSnapshot
    eligible_for_review: bool
    reasons: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_manifest(project: Path) -> dict[str, str]:
    protected: dict[str, str] = {}
    for path in project.rglob("*"):
        if path.is_symlink():
            raise ToolError("Symlinks are not allowed inside an investigation project")
        if not path.is_file():
            continue
        relative = path.relative_to(project)
        if (
            relative.parts[0] in {"seeds", "tests"}
            or relative.suffix in {".yml", ".yaml"}
            or relative.name in {"dbt_project.yml", "profiles.yml"}
        ):
            protected[relative.as_posix()] = _sha256(path)
    return protected


def _model_digest(project: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((project / "models").rglob("*.sql")):
        if path.is_symlink():
            raise ToolError("Model symlinks are not allowed")
        digest.update(path.relative_to(project).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validate_model_sql(code: str) -> None:
    if any(delimiter in code for delimiter in ("{%", "%}", "{#", "#}")):
        raise ToolError("Only ref() and source() Jinja expressions are allowed")

    def replace_expression(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if REF_CALL.fullmatch(expression) or SOURCE_CALL.fullmatch(expression):
            return "allowed_relation"
        raise ToolError("Only literal ref() and source() calls are allowed")

    plain_sql = JINJA_EXPRESSION.sub(replace_expression, code)
    if "{{" in plain_sql or "}}" in plain_sql:
        raise ToolError("Unmatched Jinja expression")
    try:
        statements = duckdb.extract_statements(plain_sql)
    except duckdb.Error as exc:
        raise ToolError("Model patch is not valid DuckDB SQL") from exc
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ToolError("Model patch must contain one SELECT statement")


def _changed_models(before: Path, after: Path) -> set[str]:
    before_models = {path.relative_to(before) for path in (before / "models").rglob("*.sql")}
    after_models = {path.relative_to(after) for path in (after / "models").rglob("*.sql")}
    changed: set[str] = set()
    for relative in before_models | after_models:
        before_path, after_path = before / relative, after / relative
        if (
            not before_path.is_file()
            or not after_path.is_file()
            or before_path.read_bytes() != after_path.read_bytes()
        ):
            changed.add(relative.stem)
    return changed


def _affected_models(project: Path, changed: set[str]) -> set[str]:
    affected = set(changed)
    references = {
        path.stem: set(REF_PATTERN.findall(path.read_text(encoding="utf-8")))
        for path in (project / "models").rglob("*.sql")
    }
    while True:
        descendants = {name for name, refs in references.items() if refs & affected}
        if descendants <= affected:
            return affected
        affected.update(descendants)


def _revenue_lineage(project: Path) -> set[str]:
    references = {
        path.stem: set(REF_PATTERN.findall(path.read_text(encoding="utf-8")))
        for path in (project / "models").rglob("*.sql")
    }
    lineage = {"fct_daily_revenue"}
    while True:
        upstream = (
            set().union(*(references.get(name, set()) for name in lineage)) & references.keys()
        )
        if upstream <= lineage:
            return lineage
        lineage.update(upstream)


class InvestigatorTools:
    def __init__(self, source_root: Path, sandbox_root: Path, image: str = IMAGE) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.sandbox_root = sandbox_root.resolve()
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.image = image
        if (
            self.source_root == self.sandbox_root
            or self.source_root in self.sandbox_root.parents
            or self.sandbox_root in self.source_root.parents
        ):
            raise ValueError("Source and sandbox roots must be separate")

    def create_sandbox(self, source_project: Path) -> SandboxHandle:
        source_project = source_project.resolve(strict=True)
        if source_project.name != "project" or not source_project.is_relative_to(self.source_root):
            raise ToolError("Scenario project is outside the allowed source root")
        if not (source_project / "dbt_project.yml").is_file():
            raise ToolError("Scenario project has no dbt project file")
        if any(path.name == "ground_truth.json" for path in source_project.rglob("*")):
            raise ToolError("Ground truth cannot enter an investigator sandbox")
        _protected_manifest(source_project)
        sandbox_id = str(uuid.uuid4())
        destination = self.sandbox_root / sandbox_id
        destination.mkdir(mode=0o700)
        project = destination / "project"
        shutil.copytree(
            source_project,
            project,
            ignore=shutil.ignore_patterns("target", "logs", "*.duckdb", "*.wal"),
        )
        state = destination / "state"
        state.mkdir(mode=0o700)
        guard = destination / "guard"
        guard.mkdir(mode=0o700)
        (guard / "protected.json").write_text(
            json.dumps(_protected_manifest(project), sort_keys=True), encoding="utf-8"
        )
        return SandboxHandle(sandbox_id=sandbox_id, project=project)

    def _paths(self, sandbox_id: str) -> tuple[Path, Path, Path]:
        try:
            if str(uuid.UUID(sandbox_id)) != sandbox_id:
                raise ValueError
        except ValueError as exc:
            raise ToolError("Invalid sandbox identifier") from exc
        base = self.sandbox_root / sandbox_id
        if base.is_symlink() or not base.is_dir() or base.resolve() != base:
            raise ToolError("Sandbox does not exist")
        project, state, guard = base / "project", base / "state", base / "guard"
        if any(path.is_symlink() or not path.is_dir() for path in (project, state, guard)):
            raise ToolError("Sandbox directory was replaced")
        return project, state, guard

    def _check_protected(self, sandbox_id: str) -> None:
        project, _, guard = self._paths(sandbox_id)
        expected = json.loads((guard / "protected.json").read_text(encoding="utf-8"))
        if _protected_manifest(project) != expected:
            raise ToolError("Protected seeds, tests, or dbt configuration changed")

    def _model_path(self, sandbox_id: str, model_path: str) -> Path:
        project, _, _ = self._paths(sandbox_id)
        relative = PurePosixPath(model_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) < 2
            or relative.parts[0] != "models"
            or relative.suffix != ".sql"
            or any(part in {"test", "tests", "macros", "seeds"} for part in relative.parts)
        ):
            raise ToolError("Only existing dbt model SQL files may be edited or read")
        if not (TEMPLATE_MODELS / Path(*relative.parts[1:])).is_file():
            raise ToolError("Model is not part of the trusted template")
        path = project / Path(*relative.parts)
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(project / "models")
        ):
            raise ToolError("Model path escapes the sandbox")
        return path

    def read_model_code(self, sandbox_id: str, model_path: str) -> str:
        return self._model_path(sandbox_id, model_path).read_text(encoding="utf-8")

    def get_lineage(self, sandbox_id: str, model_path: str) -> Lineage:
        path = self._model_path(sandbox_id, model_path)
        project, _, _ = self._paths(sandbox_id)
        upstream = tuple(sorted(set(REF_PATTERN.findall(path.read_text(encoding="utf-8")))))
        downstream_models: list[str] = []
        for candidate in (project / "models").rglob("*.sql"):
            if candidate == path:
                continue
            guarded = self._model_path(sandbox_id, candidate.relative_to(project).as_posix())
            if path.stem in REF_PATTERN.findall(guarded.read_text(encoding="utf-8")):
                downstream_models.append(candidate.stem)
        downstream = tuple(sorted(downstream_models))
        return Lineage(upstream=upstream, downstream=downstream)

    def get_recent_changes(self, sandbox_id: str, model_path: str) -> RecentChanges:
        path = self._model_path(sandbox_id, model_path)
        project, _, _ = self._paths(sandbox_id)
        relative = path.relative_to(project)
        template = ROOT / "analytics" / "template" / relative
        diff = "".join(
            difflib.unified_diff(
                template.read_text(encoding="utf-8").splitlines(keepends=True),
                path.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile=f"template/{relative}",
                tofile=f"sandbox/{relative}",
            )
        )
        result = subprocess.run(
            ["git", "log", "-5", "--format=%h %cs %s", "--", str(template.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return RecentChanges(template_diff=diff, commits=tuple(result.stdout.splitlines()))

    def apply_patch_in_sandbox(self, sandbox_id: str, model_path: str, new_code: str) -> str:
        self._check_protected(sandbox_id)
        path = self._model_path(sandbox_id, model_path)
        if not new_code.strip() or len(new_code.encode("utf-8")) > 100_000:
            raise ToolError("Model patch is empty or exceeds 100 KB")
        _validate_model_sql(new_code)
        previous = path.read_text(encoding="utf-8")
        if previous == new_code:
            raise ToolError("Model patch makes no change")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".model-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(new_code)
        os.replace(temporary, path)
        self._check_protected(sandbox_id)
        return "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                new_code.splitlines(keepends=True),
                fromfile=f"a/{model_path}",
                tofile=f"b/{model_path}",
            )
        )

    def _container(
        self,
        sandbox_id: str,
        operation: str,
        arguments: tuple[str, ...] = (),
        stdin: str | None = None,
        timeout_seconds: int = 30,
        writable_state: bool = False,
    ) -> dict[str, Any]:
        project, state, _ = self._paths(sandbox_id)
        name = f"air-{uuid.uuid4().hex}"
        state_mount = f"type=bind,source={state},target=/state"
        if not writable_state:
            state_mount += ",readonly"
        # This is an ephemeral container mount, never a host temporary directory.
        tmpfs_spec = "/tmp:rw,nosuid,nodev,size=64m"  # nosec B108
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            tmpfs_spec,
            "--mount",
            f"type=bind,source={project},target=/workspace,readonly",
            "--mount",
            state_mount,
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "DBT_DUCKDB_PATH=/state/warehouse.duckdb",
            self.image,
            operation,
            *arguments,
        ]
        if stdin is not None:
            command.insert(2, "-i")
        try:
            result = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True, check=False, timeout=5)
            raise ToolTimeout(f"{operation} exceeded {timeout_seconds} seconds") from exc
        if result.returncode:
            raise ToolError((result.stderr or result.stdout)[-2000:])
        try:
            response: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ToolError("Container returned invalid JSON") from exc
        return response

    def run_sql(
        self, sandbox_id: str, sql: str, row_limit: int = 100, timeout_seconds: int = 10
    ) -> SQLResult:
        self._check_protected(sandbox_id)
        if not 1 <= row_limit <= 1000 or not 1 <= timeout_seconds <= 30:
            raise ToolError("Row limit or timeout is outside the allowed bounds")
        if len(sql.encode("utf-8")) > 20_000:
            raise ToolError("SQL query exceeds 20 KB")
        response = self._container(sandbox_id, "query", (str(row_limit),), sql, timeout_seconds)
        return SQLResult(
            columns=tuple(response["columns"]),
            rows=tuple(tuple(row) for row in response["rows"]),
            truncated=bool(response["truncated"]),
        )

    def list_tables(self, sandbox_id: str) -> tuple[str, ...]:
        result = self.run_sql(
            sandbox_id,
            "select table_name from information_schema.tables "
            "where table_schema = 'main' order by table_name",
            row_limit=1000,
        )
        return tuple(str(row[0]) for row in result.rows)

    def describe_table(self, sandbox_id: str, table_name: str) -> tuple[ColumnInfo, ...]:
        if not TABLE_PATTERN.fullmatch(table_name):
            raise ToolError("Invalid table name")
        # The identifier cannot contain SQL syntax after this validation.
        result = self.run_sql(
            sandbox_id,
            "select column_name, data_type, is_nullable "
            "from information_schema.columns "
            f"where table_schema = 'main' and table_name = '{table_name}' "  # nosec B608
            "order by ordinal_position",
        )
        return tuple(ColumnInfo(str(row[0]), str(row[1]), row[2] == "YES") for row in result.rows)

    def run_dbt_tests(self, sandbox_id: str) -> DbtCheck:
        self._check_protected(sandbox_id)
        project, state, guard = self._paths(sandbox_id)
        response = self._container(sandbox_id, "build", timeout_seconds=180, writable_state=True)
        self._check_protected(sandbox_id)
        steps = tuple(
            DbtStep(str(step["command"]), step["returncode"] == 0, str(step["output"]))
            for step in response["steps"]
        )
        tests_passed = bool(response["tests_passed"])
        (guard / "last_test.json").write_text(
            json.dumps(
                {
                    "model_digest": _model_digest(project),
                    "database_digest": (
                        _sha256(state / "warehouse.duckdb")
                        if (state / "warehouse.duckdb").is_file()
                        else None
                    ),
                    "tests_passed": tests_passed,
                }
            ),
            encoding="utf-8",
        )
        return DbtCheck(steps=steps, tests_passed=tests_passed)

    def _snapshot(self, sandbox_id: str) -> MetricSnapshot:
        self._check_protected(sandbox_id)
        response = self._container(sandbox_id, "metrics", timeout_seconds=30)
        return MetricSnapshot(
            expected_net_usd=Decimal(str(response["expected_net_usd"])),
            actual_net_usd=(
                Decimal(str(response["actual_net_usd"]))
                if response["actual_net_usd"] is not None
                else None
            ),
            order_count=(
                int(response["actual_order_count"])
                if response["actual_order_count"] is not None
                else None
            ),
            source_order_count=int(response["source_order_count"]),
            model_rows=tuple(sorted(response["model_rows"].items())),
            pending_refunds=int(response["pending_refunds"]),
        )

    def compare_metrics(self, before_id: str, after_id: str) -> MetricComparison:
        self._check_protected(before_id)
        self._check_protected(after_id)
        before_project, _, _ = self._paths(before_id)
        after_project, after_state, after_guard = self._paths(after_id)
        if _protected_manifest(before_project) != _protected_manifest(after_project):
            raise ToolError("Before and after runs use different seeds or protected tests")
        before = self._snapshot(before_id)
        after = self._snapshot(after_id)
        test_path = after_guard / "last_test.json"
        test_record = json.loads(test_path.read_text()) if test_path.exists() else {}
        reasons: list[str] = []
        changed = _changed_models(before_project, after_project)
        if not changed:
            reasons.append("No model patch was applied")
        unrelated_patches = changed - (
            _revenue_lineage(before_project) | _revenue_lineage(after_project)
        )
        if unrelated_patches:
            reasons.append("Unrelated models patched: " + ", ".join(sorted(unrelated_patches)))
        if not test_record.get("tests_passed"):
            reasons.append("dbt tests did not pass")
        if test_record.get("model_digest") != _model_digest(after_project):
            reasons.append("Model changed after dbt tests")
        if test_record.get("database_digest") != _sha256(after_state / "warehouse.duckdb"):
            reasons.append("Warehouse changed after dbt tests")
        if after.actual_net_usd is None or abs(
            after.actual_net_usd - after.expected_net_usd
        ) > Decimal("0.005"):
            reasons.append("Net revenue does not reconcile to raw events")
        if before.actual_net_usd is not None and abs(
            before.actual_net_usd - before.expected_net_usd
        ) <= Decimal("0.005"):
            reasons.append("No pre-existing metric discrepancy")
        if after.order_count != after.source_order_count or (
            before.order_count is not None and before.order_count != after.order_count
        ):
            reasons.append("Order count changed")
        affected = _affected_models(before_project, changed) | _affected_models(
            after_project, changed
        )
        before_rows, after_rows = dict(before.model_rows), dict(after.model_rows)
        unrelated = (before_rows.keys() | after_rows.keys()) - affected
        regressions = sorted(
            name
            for name in unrelated
            if before_rows.get(name) is None or before_rows.get(name) != after_rows.get(name)
        )
        if regressions:
            reasons.append("Unrelated models changed: " + ", ".join(regressions))
        if after.pending_refunds:
            reasons.append("Refund ingestion is incomplete")
        return MetricComparison(before, after, not reasons, tuple(reasons))

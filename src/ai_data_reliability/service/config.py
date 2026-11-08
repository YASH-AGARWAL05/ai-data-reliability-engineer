"""Local service configuration shared by API and worker."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str
    run_root: Path
    sandbox_root: Path
    checkpoint_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql+psycopg://reliability@127.0.0.1:55432/reliability",
            ),
            run_root=Path(os.environ.get("RUN_ROOT", ROOT / "runtime" / "runs")).resolve(),
            sandbox_root=Path(
                os.environ.get("SANDBOX_ROOT", ROOT / "runtime" / "sandboxes")
            ).resolve(),
            checkpoint_path=Path(
                os.environ.get("CHECKPOINT_PATH", ROOT / "runtime" / "checkpoints.sqlite")
            ).resolve(),
        )

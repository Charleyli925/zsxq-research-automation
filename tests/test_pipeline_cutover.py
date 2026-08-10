from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_entrypoint_has_no_development_checkout_dependency():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_zsxq_pipeline.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "zsxq-pipeline" in completed.stdout


def test_repository_hygiene_covers_unified_active_runtime_paths():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_repository_hygiene.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

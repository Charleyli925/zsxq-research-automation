from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from zsxq_pipeline.cli import _parser


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


def test_only_unified_execution_entrypoints_are_shipped():
    parser = _parser()
    command_action = next(action for action in parser._actions if getattr(action, "choices", None))
    commands = set(command_action.choices)
    assert "download" not in commands
    assert "process" not in commands

    assert not (ROOT / "openclaw_tasks" / "zsxq_download" / "run.sh").exists()
    assert not (ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.sh").exists()
    assert not (ROOT / "deploy" / "install_local_runtime.sh").exists()
    assert not (ROOT / "scripts" / "run_zsxq_download_pipeline.py").exists()
    assert [path.name for path in (ROOT / "deploy" / "launchd").glob("*.plist.template")] == [
        "zsxq-pipeline.plist.template"
    ]

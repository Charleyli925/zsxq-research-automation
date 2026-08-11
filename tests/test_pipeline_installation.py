from __future__ import annotations

import json
import importlib.util
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from zsxq_pipeline.model import ErrorCategory, Stage
from zsxq_pipeline.state import PipelineState


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install_pipeline_runtime.py"
TEMPLATE = ROOT / "deploy" / "launchd" / "zsxq-pipeline.plist.template"
SPEC = importlib.util.spec_from_file_location("install_pipeline_runtime", INSTALLER)
assert SPEC is not None and SPEC.loader is not None
INSTALLER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER_MODULE)


def _release(tmp_path: Path, name: str) -> Path:
    release = tmp_path / name
    shutil.copytree(
        ROOT,
        release,
        ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", "__pycache__", ".runtime"),
    )
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.com"],
        ["git", "config", "user.name", "Pipeline Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", name],
        ["git", "checkout", "--detach"],
    ):
        completed = subprocess.run(argv, cwd=release, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr
    return release


def _config(tmp_path: Path, runtime: Path, *, codex: str = "/usr/bin/true") -> Path:
    path = tmp_path / "pipeline.toml"
    path.write_text(
        f'''schema_version = 1

[runtime]
root = "{runtime}"
database = "state/pipeline.sqlite3"

[schedule]
timezone = "Asia/Shanghai"

[model]
name = "test-model"
provider = "codex"
prompt_version = "test"

[codex]
command = "{codex}"
model = "test-model"
prompt_version = "test"

[lark]
command = "/usr/bin/true"

[publish_targets.daily]
kind = "lark"
target = "test"
''',
        encoding="utf-8",
    )
    return path


def _run(*args: str):
    return subprocess.run([sys.executable, str(INSTALLER), *args], capture_output=True, text=True, check=False)


def test_unified_template_is_one_shot_and_has_explicit_runtime_contract():
    payload = plistlib.loads(TEMPLATE.read_bytes())
    environment = payload["EnvironmentVariables"]
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 300
    assert "KeepAlive" not in payload
    assert payload["ProgramArguments"][-2:] == ["--config", "__CONFIG_PATH__"]
    assert {"HOME", "PATH", "ZSXQ_PIPELINE_RUNTIME_ROOT"}.issubset(environment)
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert environment["no_proxy"] == "127.0.0.1,localhost"


def test_install_and_rollback_switch_only_release_entrypoint(tmp_path):
    runtime = tmp_path / "runtime"
    config = _config(tmp_path, runtime)
    first = _release(tmp_path, "release-one")
    installed = _run(
        "install",
        "--apply",
        "--skip-launchd",
        "--release-root",
        str(first),
        "--runtime-root",
        str(runtime),
        "--config",
        str(config),
    )
    assert installed.returncode == 0, installed.stderr
    first_payload = json.loads(installed.stdout)
    assert first_payload["activated"] is True
    first_current = (runtime / "current").resolve()
    assert first_current.name == first_payload["release_sha"]

    second = _release(tmp_path, "release-two")
    updated = _run(
        "install",
        "--apply",
        "--skip-launchd",
        "--release-root",
        str(second),
        "--runtime-root",
        str(runtime),
        "--config",
        str(config),
    )
    assert updated.returncode == 0, updated.stderr
    assert (runtime / "current").resolve().name == json.loads(updated.stdout)["release_sha"]

    rollback = _run("rollback", "--apply", "--skip-launchd", "--runtime-root", str(runtime))
    assert rollback.returncode == 0, rollback.stderr
    assert (runtime / "current").resolve() == first_current
    manifest = json.loads((runtime / "deployment-manifest.json").read_text(encoding="utf-8"))
    assert "config_sha256" in manifest
    assert str(config) not in json.dumps(manifest)


def test_failed_doctor_does_not_switch_current_or_write_launchd(tmp_path):
    runtime = tmp_path / "runtime"
    config = _config(tmp_path, runtime, codex="/definitely/missing/codex")
    release = _release(tmp_path, "release-fail")
    launch_agents = tmp_path / "LaunchAgents"
    completed = _run(
        "install",
        "--apply",
        "--skip-launchd",
        "--release-root",
        str(release),
        "--runtime-root",
        str(runtime),
        "--config",
        str(config),
        "--launch-agents-dir",
        str(launch_agents),
    )
    assert completed.returncode != 0
    assert not (runtime / "current").exists()
    assert not launch_agents.exists()


def test_business_blocked_state_does_not_fail_release_capability_doctor(tmp_path):
    runtime = tmp_path / "runtime"
    config = _config(tmp_path, runtime)
    database = runtime / "state" / "pipeline.sqlite3"
    now = datetime(2026, 8, 11, tzinfo=UTC)
    with PipelineState.open(database) as state:
        state.migrate()
        document = state.upsert_document("legacy", "blocked", now=now)
        state.ensure_stage(document.id, Stage.PUBLISH, "legacy", now=now)
        claim = state.claim_due_stage(Stage.PUBLISH, "legacy", now=now)
        assert claim is not None
        state.fail_stage(claim, category=ErrorCategory.AUTH, now=now)

    release = _release(tmp_path, "release-with-business-debt")
    installed = _run(
        "install",
        "--apply",
        "--skip-launchd",
        "--release-root",
        str(release),
        "--runtime-root",
        str(runtime),
        "--config",
        str(config),
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["activated"] is True

    with PipelineState.open(database) as state:
        assert state.derive_health(now=now)["health"] == "blocked"


def test_crontab_retirement_is_exact_and_reversible(tmp_path, monkeypatch):
    state = tmp_path / "crontab.txt"
    legacy = "*/10 * * * * /legacy/digest/run.cron-safe.sh >> /legacy/digest/cron.log 2>&1"
    keep = "0 9 * * 1-5 /unrelated/task.sh"
    state.write_text(f"# retained comment\n{keep}\n\n{legacy}\n", encoding="utf-8")
    command = tmp_path / "fake-crontab"
    command.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_CRONTAB_STATE"])
if sys.argv[1:] == ["-l"]:
    sys.stdout.write(path.read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:] == ["-"]:
    path.write_text(sys.stdin.read(), encoding="utf-8")
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    monkeypatch.setenv("FAKE_CRONTAB_STATE", str(state))

    original = INSTALLER_MODULE._read_crontab(str(command))
    expected = INSTALLER_MODULE._validated_crontab_lines([legacy], original)
    updated = INSTALLER_MODULE._without_crontab_lines(original, expected)
    INSTALLER_MODULE._write_crontab(str(command), updated)
    assert legacy not in state.read_text(encoding="utf-8")
    assert keep in state.read_text(encoding="utf-8")
    INSTALLER_MODULE._write_crontab(str(command), original)
    assert state.read_text(encoding="utf-8") == original


def test_legacy_backup_names_do_not_overwrite_equal_basenames(tmp_path):
    first = tmp_path / "one" / "run.sh"
    second = tmp_path / "two" / "run.sh"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    destination = tmp_path / "backup"
    destination.mkdir()

    names = INSTALLER_MODULE._backup([first, second], destination)
    assert names == ["legacy-001-run.sh", "legacy-002-run.sh"]
    assert (destination / names[0]).read_text(encoding="utf-8") == "first\n"
    assert (destination / names[1]).read_text(encoding="utf-8") == "second\n"


def test_legacy_scheduler_presence_refuses_non_cutover_install(tmp_path):
    runtime = tmp_path / "runtime"
    config = _config(tmp_path, runtime)
    release = _release(tmp_path, "release-legacy")
    legacy = tmp_path / "legacy.plist"
    legacy.write_text("<plist/>", encoding="utf-8")
    completed = _run(
        "install",
        "--apply",
        "--skip-launchd",
        "--release-root",
        str(release),
        "--runtime-root",
        str(runtime),
        "--config",
        str(config),
        "--legacy-plist",
        str(legacy),
    )
    assert completed.returncode != 0
    assert "legacy scheduler" in completed.stderr
    assert not (runtime / "current").exists()

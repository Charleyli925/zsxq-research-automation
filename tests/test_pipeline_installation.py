from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install_pipeline_runtime.py"
TEMPLATE = ROOT / "deploy" / "launchd" / "zsxq-pipeline.plist.template"


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
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 300
    assert "KeepAlive" not in payload
    assert payload["ProgramArguments"][-2:] == ["--config", "__CONFIG_PATH__"]
    assert {"HOME", "PATH", "ZSXQ_PIPELINE_RUNTIME_ROOT"}.issubset(payload["EnvironmentVariables"])


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

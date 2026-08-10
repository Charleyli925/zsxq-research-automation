from __future__ import annotations

import json

from zsxq_pipeline.cli import main
from zsxq_pipeline.config import ConfigError, load_pipeline_config


def _write_config(path, runtime_root, legacy_root) -> None:
    path.write_text(
        f'''schema_version = 1

[runtime]
root = "{runtime_root}"
database = "state/pipeline.sqlite3"

[schedule]
timezone = "Asia/Shanghai"

[model]
name = "example-model"
provider = "example-provider"
prompt_version = "v1"

[sources.foreign]
kind = "zsxq"
state_path = "state/foreign.json"

[publish_targets.daily]
kind = "lark"
target = "logical-daily-target"

[legacy]
root = "{legacy_root}"
''',
        encoding="utf-8",
    )


def test_cli_migrate_status_and_state_only_doctor_are_local_only(tmp_path, capsys):
    database = tmp_path / "state" / "pipeline.sqlite3"
    assert main(["db", "migrate", "--database", str(database)]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["migrated"] is True

    assert main(["status", "--database", str(database), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["health"] == "healthy"
    assert status["sources"] == {}

    assert main(["doctor", "--database", str(database), "--state-only"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["database"] == str(database)
    assert doctor["ok"] is True


def test_legacy_apply_requires_explicit_apply_before_database_write(tmp_path, capsys):
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    database = tmp_path / "state" / "pipeline.sqlite3"
    plan_path = tmp_path / "plan.json"
    assert main(["legacy", "plan", "--legacy-root", str(legacy_root), "--output", str(plan_path)]) == 0
    capsys.readouterr()
    assert main(["legacy", "apply", "--plan", str(plan_path), "--database", str(database)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert not database.exists()


def test_config_rejects_runtime_root_escapes_and_unknown_fields(tmp_path):
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    config_path = tmp_path / "pipeline.toml"
    _write_config(config_path, tmp_path / "runtime", legacy_root)
    config = load_pipeline_config(config_path)
    assert config.runtime.database == (tmp_path / "runtime" / "state" / "pipeline.sqlite3")

    escaped_path = tmp_path / "escaped.toml"
    _write_config(escaped_path, tmp_path / "runtime", legacy_root)
    escaped_path.write_text(
        escaped_path.read_text(encoding="utf-8").replace('database = "state/pipeline.sqlite3"', 'database = "../escape.sqlite3"'),
        encoding="utf-8",
    )
    try:
        load_pipeline_config(escaped_path)
    except ConfigError as exc:
        assert "inside runtime.root" in str(exc)
    else:  # pragma: no cover - makes the assertion readable if validation regresses
        raise AssertionError("escaped runtime path was accepted")

    unknown_path = tmp_path / "unknown.toml"
    _write_config(unknown_path, tmp_path / "runtime", legacy_root)
    unknown_path.write_text(unknown_path.read_text(encoding="utf-8") + "\n[unexpected]\nsecret = \"do-not-log\"\n", encoding="utf-8")
    try:
        load_pipeline_config(unknown_path)
    except ConfigError as exc:
        assert "secret" not in str(exc)
        assert "unsupported" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown config table was accepted")

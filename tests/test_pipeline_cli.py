from __future__ import annotations

import json

import pytest

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

    relative_legacy_path = tmp_path / "relative-legacy.toml"
    _write_config(relative_legacy_path, tmp_path / "runtime", legacy_root)
    relative_legacy_path.write_text(
        relative_legacy_path.read_text(encoding="utf-8").replace(f'root = "{legacy_root}"', 'root = "legacy"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="legacy.root must be absolute"):
        load_pipeline_config(relative_legacy_path)


def test_direct_codex_lark_and_grouping_config_is_typed_and_bounded(tmp_path):
    runtime_root = tmp_path / "runtime"
    profile_dir = tmp_path / "lark-profile"
    config_path = tmp_path / "direct.toml"
    config_path.write_text(
        f'''schema_version = 1

[runtime]
root = "{runtime_root}"
database = "state/pipeline.sqlite3"

[codex]
command = "/Applications/Codex.app/Contents/Resources/codex"
model = "gpt-5.6-sol"
prompt_version = "summary-2026-08"
reasoning = "high"
timeout_seconds = 600
work_root = "work/summary"
prompt_path = "prompts/summary.md"
system_prompt_path = "prompts/system.md"
output_schema_path = "schemas/summary.json"

[lark]
command = "lark-cli"
config_dir = "{profile_dir}"
timeout_seconds = 90
docs_identity = "user"
notification_identity = "bot"
notifications_enabled = true
target_chat_id = "oc_test_chat"

[pipeline]
extractor_version = "extract-2026-08"
summary_max_workers = 2
doc_group_size = 10
doc_group_threshold = 15
max_files_per_document = 20

[publish_targets.daily]
kind = "lark"
target = "daily-digest"
target_document = "daily-doc-logical-id"
''',
        encoding="utf-8",
    )

    config = load_pipeline_config(config_path)
    assert config.model.provider == "codex"
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.reasoning == "high"
    assert config.codex.work_root == runtime_root / "work" / "summary"
    assert config.lark.config_dir == profile_dir
    assert config.lark.docs_identity == "user"
    assert config.lark.notification_identity == "bot"
    assert config.pipeline.summary_max_workers == 2
    assert config.pipeline.max_files_per_document == 20
    assert config.publish_targets["daily"].target_document == "daily-doc-logical-id"

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("summary_max_workers = 2", "summary_max_workers = 3"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="1..2"):
        load_pipeline_config(config_path)

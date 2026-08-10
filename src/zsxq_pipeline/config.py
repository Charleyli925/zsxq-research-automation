"""Typed TOML configuration for the direct-Codex research pipeline.

The format intentionally has no compatibility bridge to shell ``config.env``.
Legacy runtime data is read only by :mod:`zsxq_pipeline.legacy_import` through
an explicit ``[legacy]`` root.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """A TOML configuration is incomplete, ambiguous, or escapes its runtime root."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    root: Path
    database: Path


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    kind: str
    state_path: Path | None
    job_config_path: Path | None = None
    keyword_path: Path | None = None
    cdp_endpoint: str = ""
    workflow_version: str = "download:v1"
    cft_executable_path: Path | None = None
    cft_user_data_dir: Path | None = None
    cft_start_url: str = ""
    cft_headless: bool = True
    cft_window_size: str = "1440,1200"


@dataclass(frozen=True, slots=True)
class ModelConfig:
    name: str
    provider: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """A direct, argv-only ``codex exec`` runtime contract.

    Every path is rooted in ``runtime.root`` so a task configuration cannot
    accidentally expose a different local workspace to the model process.
    """

    command: str
    model: str
    prompt_version: str
    reasoning: str
    timeout_seconds: int
    work_root: Path
    prompt_path: Path | None
    system_prompt_path: Path | None
    output_schema_path: Path | None


@dataclass(frozen=True, slots=True)
class LarkConfig:
    """Explicit lark-cli identity and timeout settings for publication only."""

    command: str
    config_dir: Path | None
    timeout_seconds: int
    docs_identity: str
    notification_identity: str
    notifications_enabled: bool
    target_chat_id: str
    parent_position: str


@dataclass(frozen=True, slots=True)
class PipelineSettingsConfig:
    """Stable extraction, concurrency, and publication-grouping settings."""

    extractor_version: str
    summary_max_workers: int
    doc_group_size: int
    doc_group_threshold: int
    max_files_per_document: int


@dataclass(frozen=True, slots=True)
class PublishTargetConfig:
    name: str
    kind: str
    target: str
    target_document: str = ""


@dataclass(frozen=True, slots=True)
class LegacyConfig:
    root: Path | None


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    schema_version: int
    runtime: RuntimeConfig
    sources: dict[str, SourceConfig]
    schedule_timezone: str
    model: ModelConfig
    publish_targets: dict[str, PublishTargetConfig]
    legacy: LegacyConfig
    codex: CodexConfig
    lark: LarkConfig
    pipeline: PipelineSettingsConfig


def _as_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a TOML table")
    return value


def _require_known(table: Mapping[str, Any], *, field: str, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{field} contains unsupported field(s): {', '.join(unknown)}")


def _required_text(table: Mapping[str, Any], field: str, *, table_name: str) -> str:
    value = str(table.get(field, "")).strip()
    if not value:
        raise ConfigError(f"{table_name}.{field} is required")
    return value


def _absolute_runtime_root(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ConfigError("runtime.root is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError("runtime.root must be an absolute path")
    return path.resolve(strict=False)


def _within_root(root: Path, value: Any, *, field: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ConfigError(f"{field} is required")
    candidate = Path(raw).expanduser()
    resolved = (root / candidate).resolve(strict=False) if not candidate.is_absolute() else candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{field} must remain inside runtime.root") from exc
    return resolved


def _optional_within_root(root: Path, value: Any, *, field: str) -> Path | None:
    raw = str(value or "").strip()
    return _within_root(root, raw, field=field) if raw else None


def _optional_absolute_path(value: Any, *, field: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{field} must be an absolute path when set")
    return path.resolve(strict=False)


def _command_text(value: Any, *, field: str, default: str) -> str:
    command = str(value if value is not None else default).strip()
    if not command:
        raise ConfigError(f"{field} is required")
    if "\x00" in command:
        raise ConfigError(f"{field} may not contain a NUL byte")
    return command


def _positive_int(
    table: Mapping[str, Any],
    field: str,
    *,
    table_name: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = table.get(field, default)
    if isinstance(raw, bool):
        raise ConfigError(f"{table_name}.{field} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{table_name}.{field} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{table_name}.{field} must be in range {range_text}")
    return value


def _boolean(table: Mapping[str, Any], field: str, *, table_name: str, default: bool) -> bool:
    value = table.get(field, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{table_name}.{field} must be a boolean")
    return value


def _parse_sources(root: Path, raw: Any) -> dict[str, SourceConfig]:
    table = _as_mapping(raw if raw is not None else {}, field="sources")
    result: dict[str, SourceConfig] = {}
    for name, value in table.items():
        source_name = str(name).strip()
        if not source_name:
            raise ConfigError("sources may not contain an empty name")
        source = _as_mapping(value, field=f"sources.{source_name}")
        _require_known(
            source,
            field=f"sources.{source_name}",
            allowed={
                "kind",
                "state_path",
                "job_config",
                "keyword_file",
                "cdp_endpoint",
                "workflow_version",
                "cft_executable",
                "cft_user_data_dir",
                "cft_start_url",
                "cft_headless",
                "cft_window_size",
            },
        )
        result[source_name] = SourceConfig(
            name=source_name,
            kind=_required_text(source, "kind", table_name=f"sources.{source_name}"),
            state_path=_optional_within_root(root, source.get("state_path"), field=f"sources.{source_name}.state_path"),
            job_config_path=_optional_absolute_path(source.get("job_config"), field=f"sources.{source_name}.job_config"),
            keyword_path=_optional_absolute_path(source.get("keyword_file"), field=f"sources.{source_name}.keyword_file"),
            cdp_endpoint=str(source.get("cdp_endpoint") or "").strip(),
            workflow_version=str(source.get("workflow_version") or "download:v1").strip() or "download:v1",
            cft_executable_path=_optional_absolute_path(
                source.get("cft_executable"), field=f"sources.{source_name}.cft_executable"
            ),
            cft_user_data_dir=_optional_absolute_path(
                source.get("cft_user_data_dir"), field=f"sources.{source_name}.cft_user_data_dir"
            ),
            cft_start_url=str(source.get("cft_start_url") or "").strip(),
            cft_headless=_boolean(source, "cft_headless", table_name=f"sources.{source_name}", default=True),
            cft_window_size=str(source.get("cft_window_size") or "1440,1200").strip() or "1440,1200",
        )
    return result


def _parse_publish_targets(raw: Any) -> dict[str, PublishTargetConfig]:
    table = _as_mapping(raw if raw is not None else {}, field="publish_targets")
    result: dict[str, PublishTargetConfig] = {}
    for name, value in table.items():
        target_name = str(name).strip()
        if not target_name:
            raise ConfigError("publish_targets may not contain an empty name")
        target = _as_mapping(value, field=f"publish_targets.{target_name}")
        _require_known(target, field=f"publish_targets.{target_name}", allowed={"kind", "target", "target_document"})
        result[target_name] = PublishTargetConfig(
            name=target_name,
            kind=_required_text(target, "kind", table_name=f"publish_targets.{target_name}"),
            target=_required_text(target, "target", table_name=f"publish_targets.{target_name}"),
            target_document=str(target.get("target_document", "")).strip(),
        )
    return result


def _parse_pipeline_settings(raw: Any) -> PipelineSettingsConfig:
    table = _as_mapping(raw if raw is not None else {}, field="pipeline")
    _require_known(
        table,
        field="pipeline",
        allowed={
            "extractor_version",
            "summary_max_workers",
            "doc_group_size",
            "doc_group_threshold",
            "max_files_per_document",
        },
    )
    extractor_version = str(table.get("extractor_version", "")).strip()
    workers = _positive_int(
        table, "summary_max_workers", table_name="pipeline", default=2, minimum=1, maximum=2
    )
    group_size = _positive_int(table, "doc_group_size", table_name="pipeline", default=10)
    group_threshold = _positive_int(table, "doc_group_threshold", table_name="pipeline", default=15)
    max_files = _positive_int(table, "max_files_per_document", table_name="pipeline", default=20)
    if group_threshold < group_size:
        raise ConfigError("pipeline.doc_group_threshold must be at least pipeline.doc_group_size")
    if max_files < group_size:
        raise ConfigError("pipeline.max_files_per_document must be at least pipeline.doc_group_size")
    return PipelineSettingsConfig(
        extractor_version=extractor_version,
        summary_max_workers=workers,
        doc_group_size=group_size,
        doc_group_threshold=group_threshold,
        max_files_per_document=max_files,
    )


def _parse_lark(raw: Any) -> LarkConfig:
    table = _as_mapping(raw if raw is not None else {}, field="lark")
    _require_known(
        table,
        field="lark",
        allowed={
            "command",
            "config_dir",
            "timeout_seconds",
            "docs_identity",
            "notification_identity",
            "notifications_enabled",
            "target_chat_id",
            "parent_position",
        },
    )
    docs_identity = str(table.get("docs_identity", "user")).strip()
    notification_identity = str(table.get("notification_identity", "bot")).strip()
    if docs_identity != "user":
        raise ConfigError("lark.docs_identity must be 'user'")
    if notification_identity != "bot":
        raise ConfigError("lark.notification_identity must be 'bot'")
    return LarkConfig(
        command=_command_text(table.get("command"), field="lark.command", default="lark-cli"),
        config_dir=_optional_absolute_path(table.get("config_dir"), field="lark.config_dir"),
        timeout_seconds=_positive_int(table, "timeout_seconds", table_name="lark", default=60),
        docs_identity=docs_identity,
        notification_identity=notification_identity,
        notifications_enabled=_boolean(table, "notifications_enabled", table_name="lark", default=True),
        target_chat_id=str(table.get("target_chat_id", "")).strip(),
        parent_position=str(table.get("parent_position", "my_library")).strip() or "my_library",
    )


def _parse_model_and_codex(root: Path, model_raw: Any, codex_raw: Any) -> tuple[ModelConfig, CodexConfig]:
    model_table = _as_mapping(model_raw if model_raw is not None else {}, field="model")
    _require_known(model_table, field="model", allowed={"name", "provider", "prompt_version"})
    codex_table = _as_mapping(codex_raw if codex_raw is not None else {}, field="codex")
    _require_known(
        codex_table,
        field="codex",
        allowed={
            "command",
            "model",
            "prompt_version",
            "reasoning",
            "timeout_seconds",
            "work_root",
            "prompt_path",
            "system_prompt_path",
            "output_schema_path",
        },
    )
    if model_table:
        model = ModelConfig(
            name=_required_text(model_table, "name", table_name="model"),
            provider=_required_text(model_table, "provider", table_name="model"),
            prompt_version=_required_text(model_table, "prompt_version", table_name="model"),
        )
    else:
        # New direct-Codex configurations need not carry the legacy generic
        # provider table, but must still make the summary identity explicit.
        model = ModelConfig(
            name=_required_text(codex_table, "model", table_name="codex"),
            provider="codex",
            prompt_version=_required_text(codex_table, "prompt_version", table_name="codex"),
        )
    codex_model = str(codex_table.get("model", model.name)).strip() or model.name
    codex_prompt_version = str(codex_table.get("prompt_version", model.prompt_version)).strip() or model.prompt_version
    if model_table and "prompt_version" in codex_table and codex_prompt_version != model.prompt_version:
        raise ConfigError("codex.prompt_version must match model.prompt_version when both are configured")
    return model, CodexConfig(
        command=_command_text(codex_table.get("command"), field="codex.command", default="codex"),
        model=codex_model,
        prompt_version=codex_prompt_version,
        reasoning=str(codex_table.get("reasoning", "medium")).strip() or "medium",
        timeout_seconds=_positive_int(codex_table, "timeout_seconds", table_name="codex", default=600),
        work_root=_within_root(root, codex_table.get("work_root", "work/codex"), field="codex.work_root"),
        prompt_path=_optional_within_root(root, codex_table.get("prompt_path"), field="codex.prompt_path"),
        system_prompt_path=_optional_within_root(root, codex_table.get("system_prompt_path"), field="codex.system_prompt_path"),
        output_schema_path=_optional_within_root(root, codex_table.get("output_schema_path"), field="codex.output_schema_path"),
    )


def load_pipeline_config(path: str | Path) -> PipelineConfig:
    """Load and validate a fully structured TOML configuration.

    Paths used by the new runtime state must resolve below ``runtime.root``;
    therefore ``../`` and absolute escapes are rejected before any database is
    opened.
    """

    config_path = Path(path).expanduser().resolve(strict=True)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid pipeline TOML: {exc}") from exc
    _require_known(
        raw,
        field="root",
        allowed={
            "schema_version",
            "runtime",
            "sources",
            "schedule",
            "model",
            "codex",
            "lark",
            "pipeline",
            "publish_targets",
            "legacy",
        },
    )
    schema_version = raw.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version != 1:
        raise ConfigError("schema_version must be integer 1")

    runtime_raw = _as_mapping(raw.get("runtime"), field="runtime")
    _require_known(runtime_raw, field="runtime", allowed={"root", "database"})
    root = _absolute_runtime_root(runtime_raw.get("root"))
    runtime = RuntimeConfig(root=root, database=_within_root(root, runtime_raw.get("database"), field="runtime.database"))

    schedule_raw = _as_mapping(raw.get("schedule", {}), field="schedule")
    _require_known(schedule_raw, field="schedule", allowed={"timezone"})
    schedule_timezone = str(schedule_raw.get("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"

    model, codex = _parse_model_and_codex(root, raw.get("model"), raw.get("codex"))

    legacy_raw = _as_mapping(raw.get("legacy", {}), field="legacy")
    _require_known(legacy_raw, field="legacy", allowed={"root"})
    legacy_root_text = str(legacy_raw.get("root", "")).strip()
    legacy_root: Path | None = None
    if legacy_root_text:
        legacy_candidate = Path(legacy_root_text).expanduser()
        if not legacy_candidate.is_absolute():
            raise ConfigError("legacy.root must be absolute when set")
        legacy_root = legacy_candidate.resolve(strict=False)

    return PipelineConfig(
        schema_version=schema_version,
        runtime=runtime,
        sources=_parse_sources(root, raw.get("sources")),
        schedule_timezone=schedule_timezone,
        model=model,
        publish_targets=_parse_publish_targets(raw.get("publish_targets")),
        legacy=LegacyConfig(root=legacy_root),
        codex=codex,
        lark=_parse_lark(raw.get("lark")),
        pipeline=_parse_pipeline_settings(raw.get("pipeline")),
    )


def resolve_database(*, config_path: str | Path | None = None, database: str | Path | None = None) -> Path:
    """Resolve exactly one database input for the CLI without shell configuration."""

    if bool(config_path) == bool(database):
        raise ConfigError("provide exactly one of --config or --database")
    if config_path:
        return load_pipeline_config(config_path).runtime.database
    return Path(str(database)).expanduser().resolve(strict=False)

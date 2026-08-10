"""Typed TOML configuration for the new pipeline state core.

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


@dataclass(frozen=True, slots=True)
class ModelConfig:
    name: str
    provider: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class PublishTargetConfig:
    name: str
    kind: str
    target: str


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


def _parse_sources(root: Path, raw: Any) -> dict[str, SourceConfig]:
    table = _as_mapping(raw if raw is not None else {}, field="sources")
    result: dict[str, SourceConfig] = {}
    for name, value in table.items():
        source_name = str(name).strip()
        if not source_name:
            raise ConfigError("sources may not contain an empty name")
        source = _as_mapping(value, field=f"sources.{source_name}")
        _require_known(source, field=f"sources.{source_name}", allowed={"kind", "state_path"})
        result[source_name] = SourceConfig(
            name=source_name,
            kind=_required_text(source, "kind", table_name=f"sources.{source_name}"),
            state_path=_optional_within_root(root, source.get("state_path"), field=f"sources.{source_name}.state_path"),
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
        _require_known(target, field=f"publish_targets.{target_name}", allowed={"kind", "target"})
        result[target_name] = PublishTargetConfig(
            name=target_name,
            kind=_required_text(target, "kind", table_name=f"publish_targets.{target_name}"),
            target=_required_text(target, "target", table_name=f"publish_targets.{target_name}"),
        )
    return result


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
        allowed={"schema_version", "runtime", "sources", "schedule", "model", "publish_targets", "legacy"},
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

    model_raw = _as_mapping(raw.get("model", {}), field="model")
    _require_known(model_raw, field="model", allowed={"name", "provider", "prompt_version"})
    model = ModelConfig(
        name=_required_text(model_raw, "name", table_name="model"),
        provider=_required_text(model_raw, "provider", table_name="model"),
        prompt_version=_required_text(model_raw, "prompt_version", table_name="model"),
    )

    legacy_raw = _as_mapping(raw.get("legacy", {}), field="legacy")
    _require_known(legacy_raw, field="legacy", allowed={"root"})
    legacy_root_text = str(legacy_raw.get("root", "")).strip()
    legacy_root = Path(legacy_root_text).expanduser().resolve(strict=False) if legacy_root_text else None
    if legacy_root is not None and not legacy_root.is_absolute():  # pragma: no cover - expanduser normally preserves relative
        raise ConfigError("legacy.root must be absolute when set")

    return PipelineConfig(
        schema_version=schema_version,
        runtime=runtime,
        sources=_parse_sources(root, raw.get("sources")),
        schedule_timezone=schedule_timezone,
        model=model,
        publish_targets=_parse_publish_targets(raw.get("publish_targets")),
        legacy=LegacyConfig(root=legacy_root),
    )


def resolve_database(*, config_path: str | Path | None = None, database: str | Path | None = None) -> Path:
    """Resolve exactly one database input for the CLI without shell configuration."""

    if bool(config_path) == bool(database):
        raise ConfigError("provide exactly one of --config or --database")
    if config_path:
        return load_pipeline_config(config_path).runtime.database
    return Path(str(database)).expanduser().resolve(strict=False)

"""Timezone-safe timestamp helpers for the pipeline database."""

from __future__ import annotations

from datetime import UTC, datetime


class TimestampError(ValueError):
    """Raised when a caller tries to persist an ambiguous local timestamp."""


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError("pipeline timestamps must include a timezone offset")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso_epoch(value: datetime) -> tuple[str, int]:
    aware = require_aware(value)
    utc_value = aware.astimezone(UTC)
    return utc_value.isoformat().replace("+00:00", "Z"), int(utc_value.timestamp())


def from_iso(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    return require_aware(parsed).astimezone(UTC)

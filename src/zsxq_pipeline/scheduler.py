"""Durable schedule discovery for the one-shot local pipeline worker.

``launchd`` wakes the process frequently, but never owns business time.  This
module converts configured local clock slots into one coalesced source window
per tick and records that decision in SQLite before any browser operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import PipelineConfig, SourceConfig
from .state import PipelineState


class SchedulerError(ValueError):
    """A scheduler input would make a durable window ambiguous."""


@dataclass(frozen=True, slots=True)
class ScheduledWindow:
    """One persisted catch-up window derived from one or more missed slots."""

    source: str
    source_window_id: int
    window_start: datetime
    window_end: datetime
    due_slots: tuple[datetime, ...]
    cursor_before: datetime | None
    cursor_after: datetime
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_window_id": self.source_window_id,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "due_slot_count": len(self.due_slots),
            "cursor_before": self.cursor_before.isoformat() if self.cursor_before else None,
            "cursor_after": self.cursor_after.isoformat(),
            "truncated": self.truncated,
        }


def _utc_now() -> datetime:
    return datetime.now().astimezone()


class PipelineScheduler:
    """Discover and atomically enqueue due source windows from typed config."""

    def __init__(self, config: PipelineConfig, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.config = config
        self.clock = clock
        try:
            self.timezone = ZoneInfo(config.schedule.timezone)
        except Exception as exc:  # configuration validation normally catches this first
            raise SchedulerError(f"invalid scheduler timezone: {config.schedule.timezone!r}") from exc

    @staticmethod
    def _slot_time(value: str) -> time:
        try:
            parsed = datetime.strptime(str(value), "%H:%M")
        except ValueError as exc:  # defensive: config owns normal validation
            raise SchedulerError(f"invalid schedule slot: {value!r}") from exc
        return parsed.time()

    def _due_slots(
        self,
        source: SourceConfig,
        *,
        lower_exclusive: datetime,
        now: datetime,
    ) -> tuple[datetime, ...]:
        """Return all configured local slots in ``(lower_exclusive, now]``.

        Slots retain their configured IANA timezone rather than inheriting the
        host's locale.  Iteration includes both edge dates so a tick at a
        boundary neither skips nor repeats an exact slot.
        """

        if now.tzinfo is None or lower_exclusive.tzinfo is None:
            raise SchedulerError("scheduler timestamps must be timezone-aware")
        lower_local = lower_exclusive.astimezone(self.timezone)
        now_local = now.astimezone(self.timezone)
        day = lower_local.date()
        final_day = now_local.date()
        slots: list[datetime] = []
        while day <= final_day:
            for configured in source.schedule_times:
                candidate = datetime.combine(day, self._slot_time(configured), tzinfo=self.timezone)
                if candidate > lower_local and candidate <= now_local:
                    slots.append(candidate)
            day += timedelta(days=1)
        return tuple(sorted(slots))

    def _window_start(
        self,
        state: PipelineState,
        source: SourceConfig,
        *,
        now: datetime,
    ) -> tuple[datetime, bool]:
        """Choose the checkpoint-based start and expose any bounded truncation."""

        checkpoint = state.latest_source_checkpoint(source.name)
        lookback = int(source.max_catchup_seconds or self.config.schedule.max_catchup_seconds)
        cutoff = now - timedelta(seconds=lookback)
        if checkpoint is None:
            # A fresh scheduler has no authority to invent historical source
            # coverage.  Start at the explicit bounded bootstrap horizon and
            # surface that decision in state/output.
            return cutoff, True
        if checkpoint < cutoff:
            return cutoff, True
        return checkpoint, False

    def enqueue_due_windows(self, state: PipelineState, *, now: datetime | None = None) -> tuple[ScheduledWindow, ...]:
        """Persist at most one coalesced catch-up window per configured source."""

        instant = now if now is not None else self.clock()
        if instant.tzinfo is None:
            raise SchedulerError("scheduler clock must return an aware datetime")
        windows: list[ScheduledWindow] = []
        for source in self.config.sources.values():
            if not source.schedule_times:
                continue
            cursor = state.get_schedule_cursor(source.name)
            window_start, truncated = self._window_start(state, source, now=instant)
            # The durable schedule cursor controls *slot discovery*.  A source
            # checkpoint controls *business coverage*.  On first use, bound
            # discovery to the same explicit recovery horizon.
            discovery_lower = cursor if cursor is not None else window_start
            due_slots = self._due_slots(source, lower_exclusive=discovery_lower, now=instant)
            if not due_slots:
                continue
            source_window_id = state.schedule_source_window(
                source.name,
                window_start,
                instant,
                due_cursor=due_slots[-1],
                truncated=truncated,
                now=instant,
            )
            windows.append(
                ScheduledWindow(
                    source=source.name,
                    source_window_id=source_window_id,
                    window_start=window_start,
                    window_end=instant,
                    due_slots=due_slots,
                    cursor_before=cursor,
                    cursor_after=due_slots[-1],
                    truncated=truncated,
                )
            )
        return tuple(windows)


__all__ = ["PipelineScheduler", "ScheduledWindow", "SchedulerError"]

"""Aggregate recovery metrics computed from the event log.

This module turns the raw event stream (see `agent_recovery.core.events`)
into histograms and counters useful for SLO dashboards:

* `recovery_latency` — how long recovery took for each action.
* `retry_wait` — wall-clock seconds from the original action to its retry.
* `attempt_counts` — tool-level execution attempts (creates + retries).
* `recovery_rates` — terminal-status tallies per tool.

Metrics are computed from the event log alone — no extra instrumentation
is needed in `Runtime`. A `:memory:` database is not readable from a
separate connection, so the runtime must use a file path for events to
be available here.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from agent_recovery.core.events import EventLogEntry, EventReader, entries_of_type, seconds_between

if TYPE_CHECKING:
    from collections.abc import Iterator


# Terminal action statuses — the end state we care about for latencies.
_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {
        "action.success",
        "action.failed",
        "action.unknown",
        "action.verified_absent",
    }
)

_START_EVENT = "action.started"
_RETRY_START_EVENT = "action.started"


class MetricsCollector:
    """Compute recovery metrics from a read-only view of the event log."""

    def __init__(self, database: str | Path) -> None:
        self._database = str(database)

    def event_reader(self) -> EventReader:
        """Open a short-lived event reader. Callers should `close()` it."""
        return EventReader(self._database)

    def recovery_latency(self) -> dict[str, float | None]:
        """Seconds from `action.started` to the terminal event, per action.

        Returns a mapping of `action_id` to latency in seconds. Actions with
        only a start event (still running, or lost after start) map to `None`.
        """
        with EventReader(self._database) as reader:
            events = list(reader.filter())
        return _latencies(events)

    def retry_wait(self) -> list[float]:
        """Seconds from each original `action.started` to its retry's start.

        When an action triggers an approval (via `approval.consumed`), a new
        `action.started` event is emitted for the retry attempt. This list
        measures the wall-clock gap between the original and retry starts.
        """
        with EventReader(self._database) as reader:
            events = list(reader.filter())
        return _retry_waits(events)

    def attempt_counts(self) -> dict[str, int]:
        """Total execution attempts per tool — starts, not successes."""
        with EventReader(self._database) as reader:
            events = list(reader.filter())
        counts: dict[str, int] = defaultdict(int)
        for event in events:
            if event.event_type == _START_EVENT:
                tool_name = event.payload.get("tool_name")
                if tool_name is not None:
                    counts[tool_name] += 1
        return dict(counts)

    def recovery_rates(self) -> dict[str, dict[str, int]]:
        """Terminal-status tallies per tool.

        Returns `{tool_name: {"success": N, "failed": M, ...}}` from the
        `action.<status>` terminal events. Tools with no terminal actions
        do not appear in the result.
        """
        with EventReader(self._database) as reader:
            events = list(reader.filter())
        # Build tool_name lookup from each action's first `action.started` event.
        tool_by_action: dict[str, str | None] = {}
        for event in events:
            if event.event_type == _START_EVENT:
                action_id = event.action_id
                if action_id not in tool_by_action:
                    tool_by_action[action_id] = event.payload.get("tool_name")

        rates: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for event in events:
            if event.event_type in _TERMINAL_EVENTS:
                tool_name = tool_by_action.get(event.action_id)
                if tool_name is not None:
                    status = event.event_type.replace("action.", "")
                    rates[tool_name][status] += 1
        return {tool: dict(statuses) for tool, statuses in rates.items()}


def _group_by_action(events: list[EventLogEntry]) -> list[list[EventLogEntry]]:
    """Split a flat event list into per-action lists, preserving order."""
    buckets: dict[str, list[EventLogEntry]] = defaultdict(list)
    for event in events:
        buckets[event.action_id].append(event)
    return list(buckets.values())


def _latencies(events: list[EventLogEntry]) -> dict[str, float | None]:
    """Compute latency for every action in `events`."""
    by_action = defaultdict(list)
    for event in events:
        by_action[event.action_id].append(event)

    result: dict[str, float | None] = {}
    for action_id, action_events in by_action.items():
        starts = entries_of_type(action_events, {_START_EVENT})
        terminals = entries_of_type(action_events, set(_TERMINAL_EVENTS))
        if not starts:
            continue
        start = starts[0]
        if not terminals:
            result[action_id] = None
            continue
        terminal = min(
            terminals,
            key=lambda e: e.timestamp,
        )
        result[action_id] = seconds_between(start, terminal)
    return result


def _retry_waits(events: list[EventLogEntry]) -> list[float]:
    """Seconds between each original action start and the retry action start.

    Identifies the original action from the `retry_of` field in the retry's
    `action.started` event payload, then computes the gap.
    """
    by_action = defaultdict(list)
    for event in events:
        by_action[event.action_id].append(event)

    original_starts: dict[str, EventLogEntry] = {}
    retry_starts: list[tuple[str, EventLogEntry]] = []
    for event in events:
        if event.event_type == _START_EVENT:
            retry_of = event.payload.get("retry_of")
            if retry_of:
                retry_starts.append((retry_of, event))
            else:
                original_starts[event.action_id] = event

    waits: list[float] = []
    for original_id, retry_start in retry_starts:
        original = original_starts.get(original_id)
        if original is not None:
            gap = seconds_between(original, retry_start)
            if gap is not None:
                waits.append(gap)
    return waits

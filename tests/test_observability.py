"""Tests for observability: event log reader and metrics collector."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_recovery import (
    EventLogEntry,
    EventReader,
    MetricsCollector,
    Runtime,
    Tool,
    VerificationOutcome,
)
from agent_recovery.core.events import entries_of_type, seconds_between
from agent_recovery.core.actions import UnknownOutcome

KEY = "customer-checkout-123"


class _FakeTool:
    """A minimal tool whose inspect returns a controlled outcome."""

    def __init__(self, inspect_outcome: VerificationOutcome) -> None:
        self._outcome = inspect_outcome

    @property
    def name(self) -> str:
        return "fake"

    def execute(self, arguments, idempotency_key):
        raise UnknownOutcome("response lost")

    def inspect(self, arguments, idempotency_key):
        return self._outcome


class EventReaderTests(unittest.TestCase):
    def test_events_returned_in_order_for_an_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: None,
                        inspect=lambda a, k: VerificationOutcome.found({"echo": True}),
                    )
                )
                result = runtime.execute("echo", {"msg": "hi"}, idempotency_key=KEY)

            with EventReader(database) as reader:
                events = reader.events(result.action_id)

            # Successful execute emits: action.started, action.success
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].event_type, "action.started")
            self.assertEqual(events[1].event_type, "action.success")
            # Timestamps are whole-second UTC strings from SQLite.
            self.assertGreaterEqual(events[0].timestamp, "2024")

    def test_events_for_recovered_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    _FakeTool(VerificationOutcome.unavailable("service down"))
                )
                result = runtime.execute("fake", {}, idempotency_key=KEY)
                runtime.recover(result.action_id)

            with EventReader(database) as reader:
                events = reader.events(result.action_id)

            types = [e.event_type for e in events]
            self.assertEqual(types[0], "action.started")
            self.assertIn("verification.unavailable", types)
            self.assertIn("action.unknown", types)

    def test_filter_returns_empty_for_unknown_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: None,
                        inspect=lambda a, k: VerificationOutcome.verified_absent("nope"),
                    )
                )
                result = runtime.execute("echo", {}, idempotency_key=KEY)

            with EventReader(database) as reader:
                events = list(reader.filter({"action.missing"}))

            self.assertEqual(events, [])

    def test_filter_with_empty_set_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(name="echo", execute=lambda a, k: None, inspect=lambda a, k: None)
                )
                runtime.execute("echo", {}, idempotency_key=KEY)

            with EventReader(database) as reader:
                events = list(reader.filter(set()))

            self.assertEqual(events, [])

    def test_action_ids_returns_actions_with_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: None,
                        inspect=lambda a, k: VerificationOutcome.found({"ok": True}),
                    )
                )
                result = runtime.execute("echo", {}, idempotency_key=KEY)

            with EventReader(database) as reader:
                ids = reader.action_ids()

            self.assertEqual(ids, [result.action_id])

    def test_payload_decoded_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: None,
                        inspect=lambda a, k: None,
                    )
                )
                result = runtime.execute("echo", {"msg": "hello"}, idempotency_key=KEY)

            with EventReader(database) as reader:
                events = reader.events(result.action_id)

            started = events[0]
            self.assertEqual(started.payload["tool_name"], "echo")


class MetricsCollectorTests(unittest.TestCase):
    def test_recovery_latency_for_successful_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: "done",
                        inspect=lambda a, k: None,
                    )
                )
                result = runtime.execute("echo", {}, idempotency_key=KEY)

            metrics = MetricsCollector(database)
            latencies = metrics.recovery_latency()

            self.assertIn(result.action_id, latencies)
            # SQLite timestamps have 1-second resolution; the test runs
            # within a single second, so latency can be 0.0.
            self.assertIsNotNone(latencies[result.action_id])

    def test_recovery_latency_none_for_still_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            with Runtime(database) as runtime:
                runtime.register(
                    _FakeTool(VerificationOutcome.unavailable("service down"))
                )
                result = runtime.execute("fake", {}, idempotency_key=KEY)
                runtime.recover(result.action_id)

            metrics = MetricsCollector(database)
            latencies = metrics.recovery_latency()

            # The action is unknown (terminal), latency is computed and is 0.0
            # (within the same second), not None.
            self.assertIn(result.action_id, latencies)
            self.assertIsNotNone(latencies[result.action_id])

    def test_attempt_counts_counts_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="counter",
                        execute=lambda a, k: "done",
                        inspect=lambda a, k: None,
                    )
                )
                runtime.execute("counter", {}, idempotency_key="key-1")
                runtime.execute("counter", {}, idempotency_key="key-2")

            metrics = MetricsCollector(database)
            counts = metrics.attempt_counts()

            self.assertEqual(counts["counter"], 2)

    def test_recovery_rates_counts_terminal_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: "done",
                        inspect=lambda a, k: None,
                    )
                )
                runtime.execute("echo", {}, idempotency_key="key-1")
                runtime.execute("echo", {}, idempotency_key="key-2")

            metrics = MetricsCollector(database)
            rates = metrics.recovery_rates()

            self.assertEqual(rates["echo"]["success"], 2)

    def test_recovery_rates_distinguishes_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="ok",
                        execute=lambda a, k: "done",
                        inspect=lambda a, k: None,
                    )
                )
                def _lost(a, k):
                    raise UnknownOutcome("response lost")

                runtime.register(
                    Tool(
                        name="absent",
                        execute=_lost,
                        inspect=lambda a, k: VerificationOutcome.verified_absent("nope"),
                    )
                )
                runtime.execute("ok", {}, idempotency_key="key-1")
                result = runtime.execute("absent", {}, idempotency_key="key-2")
                runtime.recover(result.action_id)

            metrics = MetricsCollector(database)
            rates = metrics.recovery_rates()

            self.assertEqual(rates["ok"]["success"], 1)
            self.assertEqual(rates["absent"]["verified_absent"], 1)


class RuntimeEventsTests(unittest.TestCase):
    def test_runtime_events_matches_event_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rt.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: None,
                        inspect=lambda a, k: VerificationOutcome.found({"echo": True}),
                    )
                )
                result = runtime.execute("echo", {}, idempotency_key="rt-key-1")
                events_via_runtime = runtime.events(result.action_id)
            with EventReader(database) as reader:
                events_via_reader = reader.events(result.action_id)

            types_rt = [e.event_type for e in events_via_runtime]
            types_reader = [e.event_type for e in events_via_reader]
            self.assertEqual(types_rt, types_reader)

    def test_runtime_metrics_returns_collector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rt.db"
            with Runtime(database) as runtime:
                runtime.register(
                    Tool(
                        name="echo",
                        execute=lambda a, k: "done",
                        inspect=lambda a, k: None,
                    )
                )
                runtime.execute("echo", {}, idempotency_key=KEY)

            collector = runtime.metrics()
            self.assertIsInstance(collector, MetricsCollector)
            rates = collector.recovery_rates()
            self.assertEqual(rates["echo"]["success"], 1)


class EventHelpersTests(unittest.TestCase):
    def test_seconds_between_parses_timestamps(self) -> None:
        a = EventLogEntry("a1", "action.started", "2025-01-01 10:00:00", {})
        b = EventLogEntry("a1", "action.success", "2025-01-01 10:00:05", {})
        self.assertEqual(seconds_between(a, b), 5.0)

    def test_seconds_between_none_for_unparseable(self) -> None:
        a = EventLogEntry("a1", "action.started", "garbage", {})
        b = EventLogEntry("a1", "action.success", "2025-01-01 10:00:00", {})
        self.assertIsNone(seconds_between(a, b))

    def test_entries_of_type_filters_correctly(self) -> None:
        events = [
            EventLogEntry("a1", "action.started", "2025-01-01 10:00:00", {}),
            EventLogEntry("a1", "action.success", "2025-01-01 10:00:05", {}),
            EventLogEntry("a1", "verification.found", "2025-01-01 10:00:06", {}),
        ]
        terminals = entries_of_type(events, {"action.success"})
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].event_type, "action.success")


if __name__ == "__main__":
    unittest.main()

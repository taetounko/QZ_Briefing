"""Unit tests for Qt connection runtime using fake components only."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qz_briefing.kiwoom import ConnectionConfig, ConnectionState  # noqa: E402
from qz_briefing.runtime import QtConnectionRuntime  # noqa: E402


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []
        self.connect_count = 0

    def connect(self, callback: object) -> None:
        self.connect_count += 1
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()


class FakeTimer:
    def __init__(self) -> None:
        self.timeout = FakeSignal()
        self.start_calls: list[int] = []
        self.stop_count = 0
        self.active = False

    def start(self, milliseconds: int) -> None:
        self.start_calls.append(milliseconds)
        self.active = True

    def stop(self) -> None:
        self.stop_count += 1
        self.active = False

    def isActive(self) -> bool:
        return self.active


class FakeAdapter:
    def __init__(self) -> None:
        self.listeners: list[object] = []
        self.close_count = 0

    def add_login_event_listener(self, callback: object) -> None:
        self.listeners.append(callback)

    def emit_login(self, error_code: int) -> None:
        for listener in tuple(self.listeners):
            listener(error_code)

    def close(self) -> None:
        self.close_count += 1


class FakeConnectionManager:
    def __init__(self, interval: float = 1.25) -> None:
        self.config = ConnectionConfig(check_interval_seconds=interval)
        self.state = ConnectionState.DISCONNECTED
        self.start_count = 0
        self.tick_count = 0
        self.stop_count = 0
        self.login_events: list[int] = []
        self.tick_error: Exception | None = None
        self.on_tick: object | None = None
        self.on_start: object | None = None

    def start(self) -> None:
        self.start_count += 1
        if self.on_start is not None:
            self.on_start()

    def tick(self) -> None:
        self.tick_count += 1
        if self.on_tick is not None:
            self.on_tick()
        if self.tick_error is not None:
            error, self.tick_error = self.tick_error, None
            raise error

    def handle_login_event(self, error_code: int) -> None:
        self.login_events.append(error_code)

    def stop(self) -> None:
        self.stop_count += 1
        self.state = ConnectionState.STOPPED


class QtConnectionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timer = FakeTimer()
        self.adapter = FakeAdapter()
        self.manager = FakeConnectionManager()
        self.runtime = QtConnectionRuntime(self.adapter, self.manager, self.timer)

    def test_start_registers_login_listener(self) -> None:
        self.runtime.start()
        self.assertEqual(len(self.adapter.listeners), 1)

    def test_start_starts_connection_manager(self) -> None:
        self.assertTrue(self.runtime.start())
        self.assertEqual(self.manager.start_count, 1)

    def test_start_starts_timer_and_reports_status(self) -> None:
        self.runtime.start()
        self.assertTrue(self.runtime.started)
        self.assertFalse(self.runtime.stopped)
        self.assertTrue(self.runtime.timer_active)
        self.assertEqual(self.runtime.connection_state, ConnectionState.DISCONNECTED)

    def test_start_converts_seconds_to_milliseconds(self) -> None:
        self.runtime.start()
        self.assertEqual(self.timer.start_calls, [1250])

    def test_duplicate_start_does_not_duplicate_listener(self) -> None:
        self.runtime.start()
        self.runtime.start()
        self.assertEqual(len(self.adapter.listeners), 1)

    def test_duplicate_start_does_not_restart_timer_or_manager(self) -> None:
        self.runtime.start()
        self.runtime.start()
        self.assertEqual(self.timer.start_calls, [1250])
        self.assertEqual(self.manager.start_count, 1)
        self.assertEqual(self.timer.timeout.connect_count, 1)

    def test_timeout_ticks_connection_manager(self) -> None:
        self.runtime.start()
        self.timer.timeout.emit()
        self.assertEqual(self.manager.tick_count, 1)

    def test_login_event_is_forwarded(self) -> None:
        self.runtime.start()
        self.adapter.emit_login(-101)
        self.assertEqual(self.manager.login_events, [-101])

    def test_login_event_during_start_is_forwarded(self) -> None:
        self.manager.on_start = lambda: self.adapter.emit_login(0)
        self.runtime.start()
        self.assertEqual(self.manager.login_events, [0])

    def test_timeout_reentry_is_ignored(self) -> None:
        self.manager.on_tick = self.timer.timeout.emit
        self.runtime.start()
        self.timer.timeout.emit()
        self.assertEqual(self.manager.tick_count, 1)

    def test_timeout_error_is_stored(self) -> None:
        error = RuntimeError("tick failed")
        self.manager.tick_error = error
        self.runtime.start()
        self.timer.timeout.emit()
        self.assertIs(self.runtime.last_runtime_error, error)

    def test_timeout_can_run_after_error(self) -> None:
        self.manager.tick_error = RuntimeError("tick failed")
        self.runtime.start()
        self.timer.timeout.emit()
        self.timer.timeout.emit()
        self.assertEqual(self.manager.tick_count, 2)

    def test_stop_stops_timer(self) -> None:
        self.runtime.start()
        self.runtime.stop()
        self.assertEqual(self.timer.stop_count, 1)
        self.assertFalse(self.runtime.timer_active)

    def test_stop_stops_manager(self) -> None:
        self.runtime.stop()
        self.assertEqual(self.manager.stop_count, 1)
        self.assertTrue(self.runtime.stopped)

    def test_stop_closes_adapter(self) -> None:
        self.runtime.stop()
        self.assertEqual(self.adapter.close_count, 1)

    def test_stop_is_idempotent(self) -> None:
        self.runtime.stop()
        self.runtime.stop()
        self.assertEqual(self.timer.stop_count, 1)
        self.assertEqual(self.manager.stop_count, 1)
        self.assertEqual(self.adapter.close_count, 1)

    def test_timeout_after_stop_is_ignored(self) -> None:
        self.runtime.start()
        self.runtime.stop()
        self.timer.timeout.emit()
        self.assertEqual(self.manager.tick_count, 0)

    def test_login_event_after_stop_is_ignored(self) -> None:
        self.runtime.start()
        self.runtime.stop()
        self.adapter.emit_login(0)
        self.assertEqual(self.manager.login_events, [])

    def test_runtime_has_no_qapplication_quit_dependency(self) -> None:
        source = inspect.getsource(QtConnectionRuntime)
        self.assertNotIn("QApplication.quit", source)
        self.assertNotIn(".quit(", source)

    def test_runtime_does_not_store_sensitive_information(self) -> None:
        stored_names = " ".join(vars(self.runtime)).lower()
        for forbidden in ("password", "account", "credential", "certificate", "pin"):
            self.assertNotIn(forbidden, stored_names)


if __name__ == "__main__":
    unittest.main()

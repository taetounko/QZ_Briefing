"""Unit tests for the top-level QApplication entry point."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qz_briefing.__main__ import ConsoleConnectionReporter, main, run  # noqa: E402
from qz_briefing.kiwoom import (  # noqa: E402
    ConnectionConfig,
    KiwoomConnectionManager,
)


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()


class FakeApplication:
    def __init__(self, events: list[str], exit_code: int = 0) -> None:
        self.aboutToQuit = FakeSignal()
        self.events = events
        self.exit_code = exit_code

    def exec_(self) -> int:
        self.events.append("exec")
        self.aboutToQuit.emit()
        return self.exit_code


class FakeAdapter:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class FakeManager:
    def __init__(self) -> None:
        self.transitions: tuple[object, ...] = ()


class FakeRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.start_count = 0
        self.stop_count = 0
        self.stopped = False
        self.start_error: Exception | None = None
        self.start_result = True

    def start(self) -> bool:
        self.start_count += 1
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.stop_count += 1
        self.events.append("stop")


class FakeConnection:
    def __init__(self, connect_state: int) -> None:
        self.connect_state = connect_state
        self.request_count = 0

    def get_connect_state(self) -> int:
        return self.connect_state

    def request_connect(self) -> int:
        self.request_count += 1
        return 0


class MainEntryPointTests(unittest.TestCase):
    def test_main_returns_event_loop_exit_code(self) -> None:
        with patch("qz_briefing.__main__.run", return_value=9):
            self.assertEqual(main(), 9)

    def test_main_returns_one_for_startup_failure(self) -> None:
        error = io.StringIO()
        with (
            patch("qz_briefing.__main__.run", side_effect=RuntimeError("failed")),
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main(), 1)
        self.assertIn("STARTUP FAILED: RuntimeError: failed", error.getvalue())

    def test_main_returns_130_for_keyboard_interrupt(self) -> None:
        with (
            patch("qz_briefing.__main__.run", side_effect=KeyboardInterrupt),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 130)

    def test_run_assembles_components_starts_runtime_and_enters_event_loop(self) -> None:
        events: list[str] = []
        application = FakeApplication(events, exit_code=7)
        adapter = FakeAdapter()
        manager = FakeManager()
        runtime = FakeRuntime(events)
        runtime_arguments: list[object] = []

        def runtime_factory(*args: object, **kwargs: object) -> FakeRuntime:
            runtime_arguments.extend(args)
            runtime_arguments.append(kwargs["on_state_change"])
            return runtime

        result = run(
            ["qz_briefing"],
            application_factory=lambda arguments: application,
            adapter_factory=lambda: adapter,  # type: ignore[arg-type]
            manager_factory=lambda value: manager,  # type: ignore[arg-type]
            runtime_factory=runtime_factory,
        )

        self.assertEqual(result, 7)
        self.assertEqual(events[:2], ["start", "exec"])
        self.assertIs(runtime_arguments[0], adapter)
        self.assertIs(runtime_arguments[1], manager)
        self.assertIsInstance(runtime_arguments[2], ConsoleConnectionReporter)
        self.assertEqual(runtime.stop_count, 1)

    def test_application_shutdown_stops_runtime(self) -> None:
        events: list[str] = []
        application = FakeApplication(events)
        runtime = FakeRuntime(events)

        run(
            [],
            application_factory=lambda arguments: application,
            adapter_factory=FakeAdapter,  # type: ignore[arg-type]
            manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
            runtime_factory=lambda *args, **kwargs: runtime,
        )

        self.assertEqual(runtime.stop_count, 1)

    def test_runtime_start_failure_still_stops_runtime(self) -> None:
        events: list[str] = []
        runtime = FakeRuntime(events)
        runtime.start_error = RuntimeError("start failed")

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            run(
                [],
                application_factory=lambda arguments: FakeApplication(events),
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: runtime,
            )

        self.assertEqual(runtime.stop_count, 1)

    def test_event_loop_failure_still_stops_runtime(self) -> None:
        events: list[str] = []
        runtime = FakeRuntime(events)
        application = FakeApplication(events)

        def fail_event_loop() -> int:
            raise RuntimeError("event loop failed")

        application.exec_ = fail_event_loop  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "event loop failed"):
            run(
                [],
                application_factory=lambda arguments: application,
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: runtime,
            )

        self.assertEqual(runtime.stop_count, 1)

    def test_runtime_assembly_failure_closes_adapter(self) -> None:
        events: list[str] = []
        adapter = FakeAdapter()

        def fail_manager(value: object) -> FakeManager:
            raise RuntimeError("manager failed")

        with self.assertRaisesRegex(RuntimeError, "manager failed"):
            run(
                [],
                application_factory=lambda arguments: FakeApplication(events),
                adapter_factory=lambda: adapter,  # type: ignore[arg-type]
                manager_factory=fail_manager,  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: FakeRuntime(events),
            )

        self.assertEqual(adapter.close_count, 1)

    def test_connected_start_skips_commconnect_and_reports_connected(self) -> None:
        connection = FakeConnection(1)
        manager = KiwoomConnectionManager(connection)
        reporter = ConsoleConnectionReporter(manager)

        manager.start()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            reporter(object())  # type: ignore[arg-type]

        self.assertEqual(connection.request_count, 0)
        self.assertIn("CONNECTION_STATE DISCONNECTED -> CONNECTED", output.getvalue())

    def test_disconnected_start_calls_commconnect_once(self) -> None:
        connection = FakeConnection(0)
        manager = KiwoomConnectionManager(
            connection,
            ConnectionConfig(reconnect_delay_seconds=0),
        )

        manager.start()
        manager.tick()
        manager.tick()

        self.assertEqual(connection.request_count, 1)

    def test_reporter_logs_login_success_and_failure(self) -> None:
        success_connection = FakeConnection(0)
        success_manager = KiwoomConnectionManager(success_connection)
        success_manager.start()
        success_connection.connect_state = 1
        success_manager.handle_login_event(0)

        failure_connection = FakeConnection(0)
        failure_manager = KiwoomConnectionManager(failure_connection)
        failure_manager.start()
        failure_manager.handle_login_event(-101)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ConsoleConnectionReporter(success_manager)(object())  # type: ignore[arg-type]
            ConsoleConnectionReporter(failure_manager)(object())  # type: ignore[arg-type]

        self.assertIn("LOGIN SUCCESS", output.getvalue())
        self.assertIn("LOGIN FAILED", output.getvalue())


if __name__ == "__main__":
    unittest.main()

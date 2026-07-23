"""Unit tests for the top-level QApplication entry point."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qz_briefing.__main__ import (  # noqa: E402
    ConsoleConnectionReporter,
    acquire_process_lock,
    main,
    parse_cli_arguments,
    run as application_run,
)
from qz_briefing.kiwoom import (  # noqa: E402
    ConnectionConfig,
    ConnectionState,
    KiwoomConnectionManager,
)
from qz_briefing.briefing import BriefingType  # noqa: E402
from qz_briefing.notifications import DisabledNotificationService  # noqa: E402
from qz_briefing.scheduling import MarketStatus, TradingDayResult  # noqa: E402


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
        self.quit_count = 0

    def exec_(self) -> int:
        self.events.append("exec")
        self.aboutToQuit.emit()
        return self.exit_code

    def quit(self) -> None:
        self.quit_count += 1


class FakeShutdownController:
    def __init__(self, application: object, process_lock: object) -> None:
        del application
        self.process_lock = process_lock
        self.runtime: object | None = None
        self.briefing_schedulers: list[object] = []
        self.stopped = False
        self.request_count = 0

    @property
    def shutdown_started(self) -> bool:
        return self.stopped

    def schedule(self) -> bool:
        return True

    def attach_runtime(self, runtime: object) -> None:
        self.runtime = runtime

    def attach_briefing_scheduler(self, scheduler: object) -> None:
        self.briefing_schedulers.append(scheduler)

    def request_shutdown(self, reason: str) -> bool:
        del reason
        self.request_count += 1
        self.handle_application_quit()
        return True

    def handle_application_quit(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for briefing_scheduler in self.briefing_schedulers:
            briefing_scheduler.stop()  # type: ignore[attr-defined]
        if self.runtime is not None:
            self.runtime.stop()  # type: ignore[attr-defined]
        self.process_lock.unlock()  # type: ignore[attr-defined]


class FakeBriefingScheduler:
    def __init__(self, callbacks: object) -> None:
        self.callbacks = callbacks

    def schedule(self, now: datetime) -> tuple[object, ...]:
        del now
        return ()

    def stop(self) -> None:
        return None


class FakeBriefingPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, dict[str, object]]] = []

    def run(
        self, briefing_type: object, trading_date: object, **kwargs: object
    ) -> object:
        self.calls.append((briefing_type, trading_date, kwargs))
        return object()


class FakeTrQueue:
    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeSleepInhibitor:
    def start(self) -> bool: return True
    def stop(self) -> None: return None


class FakeRuntimeMonitor:
    def __init__(self, *args, **kwargs) -> None:
        self.active_briefing = None; self.recovery = None
    def start(self) -> None: return None
    def stop(self) -> None: return None
    def briefing_started(self, name) -> None: self.active_briefing = name
    def briefing_completed(self, name) -> None: self.active_briefing = None


def run(*args: object, **kwargs: object) -> int:
    """Run entry-point tests with a deterministic non-Qt shutdown controller."""
    kwargs.setdefault("shutdown_controller_factory", FakeShutdownController)
    kwargs.setdefault(
        "market_day_checker",
        lambda target_date: TradingDayResult(
            target_date, MarketStatus.OPEN, "weekday"
        ),
    )
    kwargs.setdefault("briefing_scheduler_factory", FakeBriefingScheduler)
    kwargs.setdefault("tr_queue_factory", FakeTrQueue)
    kwargs.setdefault("dashboard_factory", None)
    kwargs.setdefault("sleep_inhibitor_factory", FakeSleepInhibitor)
    kwargs.setdefault("runtime_monitor_factory", FakeRuntimeMonitor)
    kwargs.setdefault("notification_service_factory", lambda project, data, timer: DisabledNotificationService())
    kwargs.setdefault("logging_configurator", lambda root: None)
    kwargs.setdefault("clock", lambda: datetime(2026, 7, 20, 9, 0))
    return application_run(*args, **kwargs)  # type: ignore[arg-type]


class FakeAdapter:
    def __init__(self) -> None:
        self.close_count = 0
        self.connect_request_count = 0
        self.login_event_count = 0
        self.last_login_error_code: int | None = None
        self.last_connect_state: int | None = None

    def close(self) -> None:
        self.close_count += 1


class FakeManager:
    def __init__(self) -> None:
        self.transitions: tuple[object, ...] = ()
        self.state = ConnectionState.CONNECTED


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


class FakeProcessLock:
    def __init__(
        self,
        try_results: list[bool] | None = None,
        stale_result: bool = False,
    ) -> None:
        self.try_results = list(try_results or [True])
        self.stale_result = stale_result
        self.try_calls: list[int] = []
        self.remove_stale_count = 0
        self.unlock_count = 0

    def tryLock(self, timeout: int = 0) -> bool:
        self.try_calls.append(timeout)
        return self.try_results.pop(0)

    def removeStaleLockFile(self) -> bool:
        self.remove_stale_count += 1
        return self.stale_result

    def unlock(self) -> None:
        self.unlock_count += 1


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
    def test_dashboard_initialization_failure_does_not_stop_briefing_runtime(self) -> None:
        events: list[str] = []
        runtime = FakeRuntime(events)

        def fail_dashboard(**kwargs):
            raise RuntimeError("ui unavailable")

        with self.assertLogs("qz_briefing.__main__", level="ERROR"):
            exit_code = run(
                [],
                application_factory=lambda arguments: FakeApplication(events),
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: runtime,
                lock_factory=FakeProcessLock,
                dashboard_factory=fail_dashboard,
                briefing_pipeline_factory=lambda clock, queue: type(
                    "Pipeline", (), {"storage_root": Path("briefings")}
                )(),
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(runtime.start_count, 1)

    def test_manual_market_close_argument_parsing_and_invalid_value(self) -> None:
        self.assertEqual(
            parse_cli_arguments(["--run-now", "market_close"]).run_now,
            "market_close",
        )
        self.assertIsNone(parse_cli_arguments([]).run_now)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parse_cli_arguments(["--run-now", "invalid"])
        self.assertEqual(raised.exception.code, 2)

    def test_manual_market_close_runs_after_connected_start_and_shuts_down(self) -> None:
        events: list[str] = []
        pipeline = FakeBriefingPipeline()
        controllers: list[FakeShutdownController] = []

        def controller_factory(app, lock):
            controller = FakeShutdownController(app, lock)
            controllers.append(controller)
            return controller

        def unexpected_scheduler(callbacks):
            raise AssertionError("manual mode must not create the regular scheduler")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = run(
                ["--run-now", "market_close"],
                application_factory=lambda arguments: FakeApplication(events),
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: FakeRuntime(events),
                lock_factory=FakeProcessLock,
                shutdown_controller_factory=controller_factory,
                briefing_scheduler_factory=unexpected_scheduler,
                briefing_pipeline_factory=lambda clock, queue: pipeline,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(pipeline.calls[0][0], BriefingType.MARKET_CLOSE)
        self.assertTrue(pipeline.calls[0][2]["manual_validation"])
        self.assertTrue(controllers[0].stopped)
        self.assertIn("manual validation completed; shutting down", output.getvalue())

    def test_scheduler_callback_invokes_shared_briefing_pipeline(self) -> None:
        events: list[str] = []
        pipeline = FakeBriefingPipeline()

        class ImmediateBriefingScheduler(FakeBriefingScheduler):
            def schedule(self, now: datetime) -> tuple[object, ...]:
                del now
                self.callbacks["pre_market"]()  # type: ignore[index]
                return ()

        exit_code = run(
            [],
            application_factory=lambda arguments: FakeApplication(events),
            adapter_factory=FakeAdapter,  # type: ignore[arg-type]
            manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
            runtime_factory=lambda *args, **kwargs: FakeRuntime(events),
            lock_factory=FakeProcessLock,
            briefing_scheduler_factory=ImmediateBriefingScheduler,
            briefing_pipeline_factory=lambda clock, adapter: pipeline,
            clock=lambda: datetime(2026, 7, 20, 9, 5),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(pipeline.calls), 1)
        self.assertEqual(pipeline.calls[0][0].value, "pre_market")  # type: ignore[union-attr]

    def test_briefing_callback_does_not_start_after_shutdown(self) -> None:
        events: list[str] = []
        pipeline = FakeBriefingPipeline()
        schedulers: list[FakeBriefingScheduler] = []

        def make_scheduler(callbacks: object) -> FakeBriefingScheduler:
            scheduler = FakeBriefingScheduler(callbacks)
            schedulers.append(scheduler)
            return scheduler

        run(
            [],
            application_factory=lambda arguments: FakeApplication(events),
            adapter_factory=FakeAdapter,  # type: ignore[arg-type]
            manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
            runtime_factory=lambda *args, **kwargs: FakeRuntime(events),
            lock_factory=FakeProcessLock,
            briefing_scheduler_factory=make_scheduler,
            briefing_pipeline_factory=lambda clock, adapter: pipeline,
        )

        schedulers[0].callbacks["pre_market"]()  # type: ignore[index]
        self.assertEqual(pipeline.calls, [])

    def test_unknown_calendar_warns_and_continues_runtime_startup(self) -> None:
        events: list[str] = []
        application = FakeApplication(events)
        runtime = FakeRuntime(events)
        process_lock = FakeProcessLock()
        controllers: list[FakeShutdownController] = []

        def make_shutdown_controller(
            app: object, lock: object
        ) -> FakeShutdownController:
            controller = FakeShutdownController(app, lock)
            controllers.append(controller)
            return controller

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = run(
                [],
                application_factory=lambda arguments: application,
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: runtime,
                lock_factory=lambda: process_lock,
                shutdown_controller_factory=make_shutdown_controller,
                market_day_checker=lambda target_date: TradingDayResult(
                    target_date,
                    MarketStatus.UNKNOWN,
                    "unknown_calendar",
                    "KRX calendar data is incomplete for 2026",
                ),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(runtime.start_count, 1)
        self.assertIn("exec", events)
        self.assertEqual(controllers[0].request_count, 0)
        self.assertIn(
            "market calendar incomplete; continuing in warning mode",
            output.getvalue(),
        )

    def test_run_logs_process_pid(self) -> None:
        events: list[str] = []
        output = io.StringIO()
        with (
            patch("qz_briefing.__main__.os.getpid", return_value=12345),
            contextlib.redirect_stdout(output),
        ):
            run(
                [],
                application_factory=lambda arguments: FakeApplication(events),
                adapter_factory=FakeAdapter,  # type: ignore[arg-type]
                manager_factory=lambda adapter: FakeManager(),  # type: ignore[arg-type]
                runtime_factory=lambda *args, **kwargs: FakeRuntime(events),
                lock_factory=FakeProcessLock,
            )
        self.assertIn("PROCESS PID: 12345", output.getvalue())

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

    def test_main_treats_keyboard_interrupt_as_clean_user_shutdown(self) -> None:
        with (
            patch("qz_briefing.__main__.run", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)

    def test_run_assembles_components_starts_runtime_and_enters_event_loop(self) -> None:
        events: list[str] = []
        application = FakeApplication(events, exit_code=7)
        adapter = FakeAdapter()
        manager = FakeManager()
        runtime = FakeRuntime(events)
        runtime_arguments: list[object] = []
        process_lock = FakeProcessLock()

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
            lock_factory=lambda: process_lock,
        )

        self.assertEqual(result, 7)
        self.assertEqual(events[:2], ["start", "exec"])
        self.assertIs(runtime_arguments[0], adapter)
        self.assertIs(runtime_arguments[1], manager)
        self.assertIsInstance(runtime_arguments[2], ConsoleConnectionReporter)
        self.assertEqual(runtime.stop_count, 1)
        self.assertEqual(process_lock.unlock_count, 1)

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
            lock_factory=FakeProcessLock,
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
                lock_factory=FakeProcessLock,
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
                lock_factory=FakeProcessLock,
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
                lock_factory=FakeProcessLock,
            )

        self.assertEqual(adapter.close_count, 1)

    def test_second_instance_exits_before_application_and_adapter_creation(self) -> None:
        output = io.StringIO()

        def unexpected_factory(*args: object) -> object:
            raise AssertionError("component factory must not be called")

        with contextlib.redirect_stdout(output):
            exit_code = run(
                [],
                application_factory=unexpected_factory,  # type: ignore[arg-type]
                adapter_factory=unexpected_factory,  # type: ignore[arg-type]
                manager_factory=unexpected_factory,  # type: ignore[arg-type]
                runtime_factory=unexpected_factory,  # type: ignore[arg-type]
                lock_factory=lambda: FakeProcessLock([False]),
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue().strip(), "QZ BRIEFING ALREADY RUNNING")

    def test_stale_lock_is_removed_and_acquired_once(self) -> None:
        process_lock = FakeProcessLock([False, True], stale_result=True)

        self.assertTrue(acquire_process_lock(process_lock))
        self.assertEqual(process_lock.try_calls, [0, 0])
        self.assertEqual(process_lock.remove_stale_count, 1)

    def test_live_lock_cannot_be_removed(self) -> None:
        process_lock = FakeProcessLock([False], stale_result=False)

        self.assertFalse(acquire_process_lock(process_lock))
        self.assertEqual(process_lock.try_calls, [0])
        self.assertEqual(process_lock.remove_stale_count, 1)

    def test_startup_failure_releases_process_lock(self) -> None:
        process_lock = FakeProcessLock()

        with self.assertRaisesRegex(RuntimeError, "application failed"):
            run(
                [],
                application_factory=lambda arguments: (_ for _ in ()).throw(
                    RuntimeError("application failed")
                ),
                lock_factory=lambda: process_lock,
            )

        self.assertEqual(process_lock.unlock_count, 1)

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
        self.assertIn("RECONNECT_WAIT", output.getvalue())

    def test_reporter_logs_adapter_diagnostics(self) -> None:
        connection = FakeConnection(0)
        manager = KiwoomConnectionManager(connection)
        adapter = FakeAdapter()
        adapter.connect_request_count = 1
        adapter.login_event_count = 1
        adapter.last_login_error_code = 0
        adapter.last_connect_state = 1

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            reporter = ConsoleConnectionReporter(manager, adapter)  # type: ignore[arg-type]
            reporter(object())  # type: ignore[arg-type]

        self.assertIn("COMMCONNECT CALL COUNT: 1", output.getvalue())
        self.assertIn("ONEVENTCONNECT ERROR CODE: 0", output.getvalue())
        self.assertIn("GETCONNECTSTATE RESULT: 1", output.getvalue())

    def test_reporter_forwards_connection_state_after_console_reporting(self) -> None:
        connection = FakeConnection(1)
        manager = KiwoomConnectionManager(connection)
        manager.start()
        observed: list[ConnectionState] = []
        runtime = type("Runtime", (), {"connection_state": manager.state})()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ConsoleConnectionReporter(
                manager, on_connection_state=observed.append
            )(runtime)  # type: ignore[arg-type]

        self.assertIn("CONNECTION_STATE DISCONNECTED -> CONNECTED", output.getvalue())
        self.assertEqual(observed, [ConnectionState.CONNECTED])


if __name__ == "__main__":
    unittest.main()

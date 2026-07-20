"""Unit tests for local-time graceful automatic shutdown."""

from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone

from qz_briefing.__main__ import run as application_run
from qz_briefing.runtime.automatic_shutdown import (
    GracefulShutdownController,
    time_until_shutdown,
)


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()  # type: ignore[operator]


class FakeTimer:
    def __init__(self, events: list[str] | None = None) -> None:
        self.timeout = FakeSignal()
        self.events = events if events is not None else []
        self.single_shot: bool | None = None
        self.started_with: int | None = None
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int) -> None:
        self.started_with = milliseconds

    def stop(self) -> None:
        self.stop_count += 1
        self.events.append("timer.stop")


class FakeRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1
        self.events.append("runtime.stop")


class FakeLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.unlock_count = 0

    def unlock(self) -> None:
        self.unlock_count += 1
        self.events.append("lock.unlock")

    def tryLock(self, timeout: int = 0) -> bool:
        del timeout
        return True

    def removeStaleLockFile(self) -> bool:
        return False


class FakeApplication:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.quit_count = 0
        self.exec_count = 0
        self.aboutToQuit = FakeSignal()

    def quit(self) -> None:
        self.quit_count += 1
        self.events.append("application.quit")

    def exec_(self) -> int:
        self.exec_count += 1
        return 0


def make_controller(
    now: datetime,
    events: list[str] | None = None,
) -> tuple[
    GracefulShutdownController, FakeTimer, FakeRuntime, FakeLock, FakeApplication
]:
    lifecycle_events = events if events is not None else []
    timer = FakeTimer(lifecycle_events)
    runtime = FakeRuntime(lifecycle_events)
    process_lock = FakeLock(lifecycle_events)
    application = FakeApplication(lifecycle_events)

    def flush_logs() -> None:
        lifecycle_events.append("logs.flush")

    controller = GracefulShutdownController(
        application,
        process_lock,
        timer_factory=lambda: timer,
        clock=lambda: now,
        flush_logs=flush_logs,
    )
    controller.attach_runtime(runtime)
    return controller, timer, runtime, process_lock, application


def test_time_until_shutdown_before_8_pm_is_exact() -> None:
    now = datetime(2026, 7, 20, 8, 0, 0, 125000)
    assert time_until_shutdown(now) == timedelta(hours=11, minutes=59, seconds=59.875)


def test_time_until_shutdown_at_8_pm_is_immediate() -> None:
    assert time_until_shutdown(datetime(2026, 7, 20, 20, 0)) == timedelta(0)


def test_time_until_shutdown_after_8_pm_is_immediate() -> None:
    assert time_until_shutdown(datetime(2026, 7, 20, 23, 59)) == timedelta(0)


def test_time_until_shutdown_handles_midnight_and_timezone_boundary() -> None:
    korea = timezone(timedelta(hours=9))
    now = datetime(2026, 7, 20, 0, 0, tzinfo=korea)
    assert time_until_shutdown(now) == timedelta(hours=20)
    assert time_until_shutdown(now).days == 0


def test_schedule_registers_single_shot_timer_for_remaining_time() -> None:
    controller, timer, _, _, _ = make_controller(datetime(2026, 7, 20, 19, 59, 30))

    assert controller.schedule()
    assert timer.single_shot is True
    assert timer.started_with == 30_000


def test_timer_timeout_runs_graceful_shutdown_in_order() -> None:
    events: list[str] = []
    controller, timer, runtime, process_lock, application = make_controller(
        datetime(2026, 7, 20, 19, 0), events
    )
    controller.schedule()

    timer.timeout.emit()

    assert events == [
        "timer.stop",
        "runtime.stop",
        "logs.flush",
        "lock.unlock",
        "application.quit",
    ]
    assert runtime.stop_count == 1
    assert process_lock.unlock_count == 1
    assert application.quit_count == 1
    assert controller.shutdown_completed


def test_repeated_shutdown_requests_only_clean_up_once() -> None:
    controller, timer, runtime, process_lock, application = make_controller(
        datetime(2026, 7, 20, 19, 0)
    )
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        assert controller.request_shutdown("automatic shutdown requested")
        assert not controller.request_shutdown("automatic shutdown requested")

    assert timer.stop_count == 1
    assert runtime.stop_count == 1
    assert process_lock.unlock_count == 1
    assert application.quit_count == 1
    assert "shutdown already in progress" in output.getvalue()


def test_starting_at_or_after_8_pm_requests_immediate_shutdown() -> None:
    controller, timer, runtime, process_lock, application = make_controller(
        datetime(2026, 7, 20, 20, 0)
    )

    assert not controller.schedule()
    assert timer.started_with is None
    assert runtime.stop_count == 1
    assert process_lock.unlock_count == 1
    assert application.quit_count == 1


def test_entry_point_after_8_pm_exits_without_creating_kiwoom_runtime() -> None:
    events: list[str] = []
    timer = FakeTimer(events)
    process_lock = FakeLock(events)
    application = FakeApplication(events)

    def unexpected_adapter() -> object:
        raise AssertionError("Kiwoom adapter must not be created after 20:00")

    exit_code = application_run(
        [],
        application_factory=lambda arguments: application,
        adapter_factory=unexpected_adapter,  # type: ignore[arg-type]
        lock_factory=lambda: process_lock,
        shutdown_controller_factory=lambda app, lock: GracefulShutdownController(
            app,
            lock,
            timer_factory=lambda: timer,
            clock=lambda: datetime(2026, 7, 20, 20, 1),
            flush_logs=lambda: events.append("logs.flush"),
        ),
    )

    assert exit_code == 0
    assert application.exec_count == 0
    assert application.quit_count == 1
    assert process_lock.unlock_count == 1


def test_ordinary_application_quit_uses_cleanup_without_quitting_again() -> None:
    controller, _, runtime, process_lock, application = make_controller(
        datetime(2026, 7, 20, 19, 0)
    )

    controller.handle_application_quit()

    assert runtime.stop_count == 1
    assert process_lock.unlock_count == 1
    assert application.quit_count == 0

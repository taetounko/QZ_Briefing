"""Local-time automatic shutdown calculation and Qt lifecycle coordination."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Protocol


AUTOMATIC_SHUTDOWN_TIME = time(hour=20)


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class ShutdownTimerLike(Protocol):
    timeout: SignalLike

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, milliseconds: int) -> None: ...

    def stop(self) -> None: ...


class ApplicationLike(Protocol):
    def quit(self) -> None: ...


class RuntimeLike(Protocol):
    def stop(self) -> None: ...


class ProcessLockLike(Protocol):
    def unlock(self) -> None: ...


ShutdownTimerFactory = Callable[[], ShutdownTimerLike]
LocalClock = Callable[[], datetime]


def time_until_shutdown(
    now: datetime,
    shutdown_time: time = AUTOMATIC_SHUTDOWN_TIME,
) -> timedelta:
    """Return today's remaining local time, clamped to zero at/after shutdown."""
    deadline = datetime.combine(now.date(), shutdown_time, tzinfo=now.tzinfo)
    return max(deadline - now, timedelta(0))


def create_shutdown_timer() -> ShutdownTimerLike:
    """Create the application-owned single-shot Qt timer lazily."""
    from PyQt5.QtCore import QTimer

    return QTimer()


def flush_log_handlers() -> None:
    """Flush all registered logging handlers without closing them."""
    seen: set[int] = set()
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            handler.flush()


class GracefulShutdownController:
    """Schedule and perform application shutdown exactly once."""

    def __init__(
        self,
        application: ApplicationLike,
        process_lock: ProcessLockLike,
        *,
        timer_factory: ShutdownTimerFactory = create_shutdown_timer,
        clock: LocalClock = datetime.now,
        flush_logs: Callable[[], None] = flush_log_handlers,
    ) -> None:
        self._application = application
        self._process_lock = process_lock
        self._timer = timer_factory()
        self._clock = clock
        self._flush_logs = flush_logs
        self._runtime: RuntimeLike | None = None
        self._briefing_resources: list[RuntimeLike] = []
        self._shutdown_started = False
        self._shutdown_completed = False
        self._lock_released = False
        self._timer.timeout.connect(self._request_automatic_shutdown)
        self._timer.setSingleShot(True)

    @property
    def shutdown_started(self) -> bool:
        return self._shutdown_started

    @property
    def shutdown_completed(self) -> bool:
        return self._shutdown_completed

    def attach_runtime(self, runtime: RuntimeLike) -> None:
        """Attach the runtime that must be stopped before QApplication."""
        self._runtime = runtime

    def attach_briefing_scheduler(self, scheduler: RuntimeLike) -> None:
        """Attach briefing timers so no new work starts during shutdown."""
        if scheduler not in self._briefing_resources:
            self._briefing_resources.append(scheduler)

    def schedule(self) -> bool:
        """Schedule 20:00 shutdown; return False when shutdown is already due."""
        remaining = time_until_shutdown(self._clock())
        if remaining <= timedelta(0):
            self.request_shutdown("automatic shutdown requested")
            return False

        delay_ms = math.ceil(remaining.total_seconds() * 1000)
        self._timer.start(delay_ms)
        print(f"automatic shutdown scheduled in {delay_ms} ms", flush=True)
        return True

    def request_shutdown(self, reason: str, *, quit_application: bool = True) -> bool:
        """Stop resources, flush logs, release the lock, and optionally quit Qt."""
        if self._shutdown_started:
            print("shutdown already in progress", flush=True)
            return False

        self._shutdown_started = True
        print(reason, flush=True)
        failures: list[str] = []
        def cleanup(label: str, callback: Callable[[], None]) -> None:
            try: callback()
            except Exception as exc:
                failures.append(f"{label}: {type(exc).__name__}")
                logging.getLogger(__name__).exception("shutdown cleanup failed: %s", label)
        for index, briefing_resource in enumerate(self._briefing_resources):
            cleanup(f"briefing_resource_{index}", briefing_resource.stop)
        cleanup("automatic_timer", self._timer.stop)
        if self._runtime is not None: cleanup("runtime", self._runtime.stop)
        cleanup("log_flush", self._flush_logs)
        if not self._lock_released:
            cleanup("process_lock", self._process_lock.unlock)
            self._lock_released = True
        if quit_application: cleanup("application_quit", self._application.quit)
        self._shutdown_completed = not failures
        print("graceful shutdown completed" if not failures else "graceful shutdown completed with cleanup errors", flush=True)
        return not failures

    def handle_application_quit(self) -> None:
        """Route ordinary QApplication termination through the same cleanup."""
        self.request_shutdown("application shutdown requested", quit_application=False)

    def _request_automatic_shutdown(self) -> None:
        self.request_shutdown("automatic shutdown requested")

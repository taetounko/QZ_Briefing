"""Qt lifecycle glue for the connection-only Kiwoom components."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from qz_briefing.kiwoom import KiwoomConnectionManager, KiwoomQAxAdapter


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class TimerLike(Protocol):
    timeout: SignalLike

    def start(self, milliseconds: int) -> None: ...

    def stop(self) -> None: ...

    def isActive(self) -> bool: ...


TimerFactory = Callable[[], TimerLike]
StateChangeCallback = Callable[["QtConnectionRuntime"], None]


def create_qtimer() -> TimerLike:
    """Create a real QTimer lazily under an existing QApplication."""
    from PyQt5.QtCore import QTimer

    return QTimer()


class QtConnectionRuntime:
    """Own timer-driven connection monitoring, but not QApplication itself."""

    def __init__(
        self,
        adapter: KiwoomQAxAdapter,
        connection_manager: KiwoomConnectionManager,
        timer: TimerLike | None = None,
        timer_factory: TimerFactory | None = None,
        on_state_change: StateChangeCallback | None = None,
    ) -> None:
        if timer is not None and timer_factory is not None:
            raise ValueError("Provide either timer or timer_factory, not both")

        self._adapter = adapter
        self._connection_manager = connection_manager
        self._timer = timer if timer is not None else (timer_factory or create_qtimer)()
        self._on_state_change = on_state_change
        self._started = False
        self._stopped = False
        self._listener_registered = False
        self._timeout_connected = False
        self._checking = False
        self._last_runtime_error: Exception | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def timer_active(self) -> bool:
        return bool(self._timer.isActive())

    @property
    def last_runtime_error(self) -> Exception | None:
        return self._last_runtime_error

    @property
    def connection_state(self) -> object:
        return self._connection_manager.state

    def start(self) -> bool:
        """Wire components and start monitoring exactly once."""
        if self._started:
            return True
        if self._stopped:
            return False

        if not self._listener_registered:
            self._adapter.add_login_event_listener(self._handle_login_event)
            self._listener_registered = True
        if not self._timeout_connected:
            self._timer.timeout.connect(self._handle_timeout)
            self._timeout_connected = True

        # CommConnect normally completes asynchronously, but the OCX may deliver
        # OnEventConnect while start() is still on the stack.  Mark the runtime
        # active before asking the manager to connect so that event is not lost.
        self._started = True
        try:
            self._connection_manager.start()
            interval_ms = round(min(1.0, self._connection_manager.config.check_interval_seconds) * 1000)
            self._timer.start(interval_ms)
        except Exception:
            self._started = False
            raise
        self._notify_state_change()
        return True

    def stop(self) -> None:
        """Stop owned components without terminating QApplication."""
        if self._stopped:
            return

        self._started = False
        self._stopped = True
        self._timer.stop()
        self._connection_manager.stop()
        self._adapter.close()
        self._notify_state_change()

    def _handle_timeout(self) -> None:
        if not self._started or self._stopped or self._checking:
            return

        self._checking = True
        try:
            self._connection_manager.tick()
            self._notify_state_change()
        except Exception as exc:
            self._last_runtime_error = exc
            self._notify_state_change()
        finally:
            self._checking = False

    def _handle_login_event(self, error_code: int) -> None:
        if not self._started or self._stopped:
            return
        try:
            self._connection_manager.handle_login_event(error_code)
            self._notify_state_change()
        except Exception as exc:
            self._last_runtime_error = exc
            self._notify_state_change()

    def _notify_state_change(self) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(self)
        except Exception as exc:
            self._last_runtime_error = exc

"""Connection-gated dispatch for scheduled briefing callbacks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from qz_briefing.briefing import BriefingType
from qz_briefing.kiwoom import ConnectionState
from qz_briefing.kiwoom import KiwoomTrClosedError, KiwoomTrTimeoutError


LOGGER = logging.getLogger(__name__)
BriefingCallback = Callable[[], None]
DispatchKey = tuple[date, BriefingType]


def create_retry_timer():
    from PyQt5.QtCore import QTimer
    return QTimer()


class ConnectionAwareBriefingDispatcher:
    """Run scheduled work once, after a confirmed CONNECTED state."""

    def __init__(
        self,
        connection_state: Callable[[], ConnectionState],
        shutdown_started: Callable[[], bool],
        retry_timer_factory: Callable[[], object] = create_retry_timer,
        retry_delay_ms: int = 60_000,
    ) -> None:
        self._connection_state = connection_state
        self._shutdown_started = shutdown_started
        self._pending: dict[DispatchKey, BriefingCallback] = {}
        self._dispatched: set[DispatchKey] = set()
        self._running: set[DispatchKey] = set()
        self._retry_counts: dict[DispatchKey, int] = {}
        self._recoverable: dict[DispatchKey, bool] = {}
        self._states: dict[DispatchKey, str] = {}
        self._retry_timer_factory, self._retry_delay_ms = retry_timer_factory, retry_delay_ms
        self._retry_timers: list[object] = []
        self._stopped = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def dispatch(
        self,
        trading_date: date,
        briefing_type: BriefingType,
        callback: BriefingCallback,
        *, recoverable: bool = True,
    ) -> bool:
        key = (trading_date, briefing_type)
        if self._is_shutting_down() or key in self._dispatched or key in self._running:
            return False
        self._recoverable[key] = recoverable
        if self._connection_state() is not ConnectionState.CONNECTED:
            self._pending.setdefault(key, callback)
            self._states[key] = "waiting_for_connection"
            print(f"briefing pending for connection: {briefing_type.value}", flush=True)
            return False
        self._run_once(key, callback)
        return True

    def on_connection_state(self, state: ConnectionState) -> None:
        if state is not ConnectionState.CONNECTED or self._is_shutting_down():
            return
        pending = tuple(self._pending.items())
        self._pending.clear()
        for key, callback in pending:
            self._run_once(key, callback)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._pending.clear()
        self._running.clear()
        for timer in self._retry_timers: timer.stop()
        self._retry_timers.clear()

    def _run_once(self, key: DispatchKey, callback: BriefingCallback) -> None:
        if self._is_shutting_down() or key in self._dispatched or key in self._running:
            return
        self._running.add(key); self._states[key] = "running"
        try:
            callback()
            self._dispatched.add(key); self._states[key] = "completed"
        except Exception as exc:
            LOGGER.exception("connected briefing dispatch failed: %s", key[1].value)
            retries = self._retry_counts.get(key, 0)
            retryable = isinstance(exc, (KiwoomTrTimeoutError, KiwoomTrClosedError, TimeoutError, ConnectionError))
            if self._recoverable.get(key, True) and retryable and retries < 1 and not self._is_shutting_down():
                self._retry_counts[key] = retries + 1
                self._pending[key] = callback; self._states[key] = "retry_wait"
                print(f"briefing retry waiting for recovery: {key[1].value}", flush=True)
                timer = self._retry_timer_factory(); timer.setSingleShot(True)
                timer.timeout.connect(lambda key=key: self._retry_due(key)); timer.start(self._retry_delay_ms)
                self._retry_timers.append(timer)
            else:
                self._states[key] = "failed"
        finally:
            self._running.discard(key)

    def _retry_due(self, key: DispatchKey) -> None:
        if self._is_shutting_down() or key not in self._pending or self._connection_state() is not ConnectionState.CONNECTED: return
        callback = self._pending.pop(key); self._run_once(key, callback)

    def _is_shutting_down(self) -> bool:
        return self._stopped or self._shutdown_started()

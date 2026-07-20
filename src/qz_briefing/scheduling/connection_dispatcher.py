"""Connection-gated dispatch for scheduled briefing callbacks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from qz_briefing.briefing import BriefingType
from qz_briefing.kiwoom import ConnectionState


LOGGER = logging.getLogger(__name__)
BriefingCallback = Callable[[], None]
DispatchKey = tuple[date, BriefingType]


class ConnectionAwareBriefingDispatcher:
    """Run scheduled work once, after a confirmed CONNECTED state."""

    def __init__(
        self,
        connection_state: Callable[[], ConnectionState],
        shutdown_started: Callable[[], bool],
    ) -> None:
        self._connection_state = connection_state
        self._shutdown_started = shutdown_started
        self._pending: dict[DispatchKey, BriefingCallback] = {}
        self._dispatched: set[DispatchKey] = set()
        self._stopped = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def dispatch(
        self,
        trading_date: date,
        briefing_type: BriefingType,
        callback: BriefingCallback,
    ) -> bool:
        key = (trading_date, briefing_type)
        if self._is_shutting_down() or key in self._dispatched:
            return False
        if self._connection_state() is not ConnectionState.CONNECTED:
            self._pending.setdefault(key, callback)
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

    def _run_once(self, key: DispatchKey, callback: BriefingCallback) -> None:
        if self._is_shutting_down() or key in self._dispatched:
            return
        self._dispatched.add(key)
        try:
            callback()
        except Exception:
            LOGGER.exception("connected briefing dispatch failed: %s", key[1].value)

    def _is_shutting_down(self) -> bool:
        return self._stopped or self._shutdown_started()

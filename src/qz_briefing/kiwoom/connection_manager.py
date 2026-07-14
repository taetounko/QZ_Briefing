"""Testable Kiwoom connection monitoring and bounded reconnection logic."""

from __future__ import annotations

import time
from collections.abc import Callable

from .connection_types import (
    ConnectionConfig,
    ConnectionState,
    ConnectionTransition,
    KiwoomConnection,
)


Clock = Callable[[], float]


class KiwoomConnectionManager:
    """Manage connection state without depending on Qt or a concrete OCX."""

    def __init__(
        self,
        connection: KiwoomConnection,
        config: ConnectionConfig | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._connection = connection
        self._config = config or ConnectionConfig()
        self._clock = clock
        self._state = ConnectionState.DISCONNECTED
        self._transitions: list[ConnectionTransition] = []
        self._reconnect_attempts = 0
        self._reconnect_due_at: float | None = None
        self._last_check_at: float | None = None
        self._request_in_flight = False
        self._current_request_is_reconnect = False
        self._started = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    @property
    def transitions(self) -> tuple[ConnectionTransition, ...]:
        return tuple(self._transitions)

    def start(self) -> None:
        """Inspect the current state and issue at most one initial request."""
        if self._state is ConnectionState.STOPPED or self._started:
            return

        self._started = True
        self._last_check_at = self._clock()
        connect_state = self._read_connect_state("initial connection check")
        if connect_state is None:
            return
        if connect_state == 1:
            self._mark_connected("already connected at startup")
            return

        self._issue_initial_request()

    def tick(self) -> None:
        """Advance monitoring using the injected clock without sleeping."""
        if not self._started or self._state in {
            ConnectionState.FAILED,
            ConnectionState.STOPPED,
        }:
            return

        now = self._clock()
        if self._state is ConnectionState.CONNECTED:
            if self._last_check_at is None:
                self._last_check_at = now
                return
            if now - self._last_check_at < self._config.check_interval_seconds:
                return

            self._last_check_at = now
            connect_state = self._read_connect_state("periodic connection check")
            if connect_state == 0:
                self._schedule_reconnect("connection loss detected")
            return

        if self._state is ConnectionState.RECONNECT_WAIT:
            if self._reconnect_due_at is not None and now >= self._reconnect_due_at:
                self._issue_reconnect_request()
            return

        # CONNECTING deliberately does nothing here so a pending request cannot
        # be duplicated by repeated timer ticks.

    def handle_login_event(self, error_code: int) -> None:
        """Apply one login event received by a future concrete OCX adapter."""
        if self._state is not ConnectionState.CONNECTING:
            return

        self._request_in_flight = False
        if int(error_code) != 0:
            self._handle_request_failure("login event reported an error")
            return

        connect_state = self._read_connect_state("post-login connection check")
        if connect_state is None:
            return
        if connect_state == 1:
            self._mark_connected("login event confirmed connected state")
            return

        self._handle_request_failure(
            "login event succeeded but connection state remained disconnected"
        )

    def stop(self) -> None:
        """Stop permanently and prevent all later connection requests."""
        if self._state is ConnectionState.STOPPED:
            return

        self._started = False
        self._request_in_flight = False
        self._reconnect_due_at = None
        self._transition(ConnectionState.STOPPED, "connection manager stopped")

    def _issue_initial_request(self) -> None:
        if self._request_in_flight or self._state is ConnectionState.STOPPED:
            return

        self._current_request_is_reconnect = False
        self._request_in_flight = True
        self._transition(
            ConnectionState.CONNECTING,
            "startup state was disconnected; initial request started",
        )
        self._call_request_connect()

    def _issue_reconnect_request(self) -> None:
        if self._request_in_flight or self._state is ConnectionState.STOPPED:
            return
        if self._reconnect_attempts >= self._config.max_reconnect_attempts:
            self._transition(
                ConnectionState.FAILED,
                "maximum reconnect attempts reached",
            )
            return

        self._reconnect_attempts += 1
        self._current_request_is_reconnect = True
        self._request_in_flight = True
        self._reconnect_due_at = None
        self._transition(
            ConnectionState.CONNECTING,
            f"reconnect attempt {self._reconnect_attempts} started",
        )
        self._call_request_connect()

    def _call_request_connect(self) -> None:
        try:
            immediate_result = int(self._connection.request_connect())
        except Exception as exc:
            self._request_in_flight = False
            self._handle_request_failure(
                f"connection request raised {type(exc).__name__}"
            )
            return

        if immediate_result != 0:
            self._request_in_flight = False
            self._handle_request_failure("connection request was rejected immediately")

    def _handle_request_failure(self, reason: str) -> None:
        self._request_in_flight = False
        if (
            self._current_request_is_reconnect
            and self._reconnect_attempts >= self._config.max_reconnect_attempts
        ):
            self._transition(ConnectionState.FAILED, reason)
            return

        self._schedule_reconnect(reason)

    def _schedule_reconnect(self, reason: str) -> None:
        if self._state is ConnectionState.STOPPED:
            return

        self._request_in_flight = False
        self._reconnect_due_at = (
            self._clock() + self._config.reconnect_delay_seconds
        )
        self._transition(ConnectionState.RECONNECT_WAIT, reason)

    def _mark_connected(self, reason: str) -> None:
        self._request_in_flight = False
        self._current_request_is_reconnect = False
        self._reconnect_attempts = 0
        self._reconnect_due_at = None
        self._last_check_at = self._clock()
        self._transition(ConnectionState.CONNECTED, reason)

    def _read_connect_state(self, context: str) -> int | None:
        try:
            connect_state = int(self._connection.get_connect_state())
        except Exception as exc:
            self._transition(
                ConnectionState.FAILED,
                f"{context} raised {type(exc).__name__}",
            )
            return None

        if connect_state not in (0, 1):
            self._transition(
                ConnectionState.FAILED,
                f"{context} returned an invalid state",
            )
            return None
        return connect_state

    def _transition(self, new_state: ConnectionState, reason: str) -> None:
        if new_state is self._state:
            return

        previous_state = self._state
        self._state = new_state
        self._transitions.append(
            ConnectionTransition(
                previous_state=previous_state,
                new_state=new_state,
                reason=reason,
                reconnect_attempts=self._reconnect_attempts,
            )
        )

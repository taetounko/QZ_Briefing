"""Testable Kiwoom connection monitoring with one login request per process."""

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
        self._login_request_attempted = False
        self._login_deadline: float | None = None
        self._started = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def config(self) -> ConnectionConfig:
        """Return the immutable timing and retry configuration."""
        return self._config

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
        if self._state is ConnectionState.CONNECTING:
            if self._login_deadline is not None and now >= self._login_deadline:
                self._fail("login response timed out")
            return

        if self._state is ConnectionState.CONNECTED:
            if self._last_check_at is None:
                self._last_check_at = now
                return
            if now - self._last_check_at < self._config.check_interval_seconds:
                return

            self._last_check_at = now
            connect_state = self._read_connect_state("periodic connection check")
            if connect_state == 0:
                self._fail("connection loss detected; restart required")
            return

    def handle_login_event(self, error_code: int) -> None:
        """Apply one login event received by a future concrete OCX adapter."""
        if self._state is not ConnectionState.CONNECTING:
            return

        self._request_in_flight = False
        self._login_deadline = None
        if int(error_code) != 0:
            self._fail("login event reported an error; restart required")
            return

        connect_state = self._read_connect_state("post-login connection check")
        if connect_state is None:
            return
        if connect_state == 1:
            self._mark_connected("login event confirmed connected state")
            return

        self._fail(
            "login event succeeded but connection state remained disconnected; "
            "restart required"
        )

    def stop(self) -> None:
        """Stop permanently and prevent all later connection requests."""
        if self._state is ConnectionState.STOPPED:
            return

        self._started = False
        self._request_in_flight = False
        self._reconnect_due_at = None
        self._login_deadline = None
        self._transition(ConnectionState.STOPPED, "connection manager stopped")

    def _issue_initial_request(self) -> None:
        if (
            self._request_in_flight
            or self._login_request_attempted
            or self._state is ConnectionState.STOPPED
        ):
            return

        self._current_request_is_reconnect = False
        self._login_request_attempted = True
        self._request_in_flight = True
        self._login_deadline = self._clock() + self._config.login_timeout_seconds
        self._transition(
            ConnectionState.CONNECTING,
            "startup state was disconnected; initial request started",
        )
        self._call_request_connect()

    def _call_request_connect(self) -> None:
        try:
            immediate_result = int(self._connection.request_connect())
        except Exception as exc:
            self._fail(
                f"connection request raised {type(exc).__name__}; restart required"
            )
            return

        if immediate_result != 0:
            self._fail("connection request was rejected immediately; restart required")

    def _fail(self, reason: str) -> None:
        self._request_in_flight = False
        self._login_deadline = None
        self._reconnect_due_at = None
        self._transition(ConnectionState.FAILED, reason)

    def _mark_connected(self, reason: str) -> None:
        self._request_in_flight = False
        self._current_request_is_reconnect = False
        self._reconnect_attempts = 0
        self._reconnect_due_at = None
        self._login_deadline = None
        self._last_check_at = self._clock()
        self._transition(ConnectionState.CONNECTED, reason)

    def _read_connect_state(self, context: str) -> int | None:
        try:
            connect_state = int(self._connection.get_connect_state())
        except Exception as exc:
            self._fail(f"{context} raised {type(exc).__name__}")
            return None

        if connect_state not in (0, 1):
            self._fail(f"{context} returned an invalid state")
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

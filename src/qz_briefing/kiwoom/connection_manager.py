"""Recoverable, Qt-independent Kiwoom connection state machine."""

from __future__ import annotations

import time
from collections.abc import Callable

from .connection_types import ConnectionConfig, ConnectionState, ConnectionTransition, KiwoomConnection

Clock = Callable[[], float]


class KiwoomConnectionManager:
    def __init__(self, connection: KiwoomConnection, config: ConnectionConfig | None = None, clock: Clock = time.monotonic) -> None:
        self._connection, self._config, self._clock = connection, config or ConnectionConfig(), clock
        self._state = ConnectionState.DISCONNECTED
        self._transitions: list[ConnectionTransition] = []
        self._reconnect_attempts = 0
        self._reconnect_due_at: float | None = None
        self._last_check_at: float | None = None
        self._request_in_flight = False
        self._login_request_attempted = False
        self._login_deadline: float | None = None
        self._recheck_due_at: float | None = None
        self._recheck_index = 0
        self._started = False

    @property
    def state(self) -> ConnectionState: return self._state
    @property
    def config(self) -> ConnectionConfig: return self._config
    @property
    def reconnect_attempts(self) -> int: return self._reconnect_attempts
    @property
    def transitions(self) -> tuple[ConnectionTransition, ...]: return tuple(self._transitions)

    def start(self) -> None:
        if self._started or self._state is ConnectionState.STOPPED: return
        self._started = True; self._last_check_at = self._clock()
        state = self._read_connect_state("initial connection check")
        if state == 1: self._mark_connected("already connected at startup")
        elif state == 0: self._issue_initial_request()

    def tick(self) -> None:
        if not self._started or self._state in {ConnectionState.FAILED, ConnectionState.STOPPED}: return
        now = self._clock()
        if self._state is ConnectionState.RECHECKING:
            if self._recheck_due_at is not None and now >= self._recheck_due_at: self._perform_recheck()
            return
        if self._state is ConnectionState.RECONNECT_WAIT:
            if self._reconnect_due_at is not None and now >= self._reconnect_due_at: self._attempt_reconnect()
            return
        if self._state in {ConnectionState.CONNECTING, ConnectionState.RECONNECTING}:
            if self._login_deadline is not None and now >= self._login_deadline:
                if self._state is ConnectionState.RECONNECTING: self._schedule_reconnect("reconnect response timed out")
                else: self._schedule_reconnect("login response timed out")
            return
        if self._state is ConnectionState.CONNECTED and self._last_check_at is not None and now - self._last_check_at >= self._config.check_interval_seconds:
            self._last_check_at = now
            if self._read_connect_state("periodic connection check") == 0:
                self._schedule_reconnect("connection state recheck confirmed disconnected")

    def handle_login_event(self, error_code: int) -> None:
        if not self._started or self._state in {ConnectionState.STOPPED, ConnectionState.FAILED}: return
        self._request_in_flight = False; self._login_deadline = None
        state = self._read_connect_state("login event connection check")
        if int(error_code) < 0:
            if state == 1:
                if self._state is not ConnectionState.RECHECKING:
                    print("connection event/state mismatch detected", flush=True)
                    self._begin_recheck()
                return
            if state == 0: self._schedule_reconnect("negative login event confirmed disconnected")
            return
        if state == 1:
            reconnecting = self._state in {ConnectionState.RECONNECTING, ConnectionState.RECONNECT_WAIT}
            self._mark_connected("automatic reconnect succeeded" if reconnecting or self._reconnect_attempts else "login event confirmed connected state")
        elif state == 0:
            self._schedule_reconnect("login event succeeded but connection remained disconnected")

    def request_connection_recheck(self, reason: str = "connection recheck requested") -> None:
        if not self._started or self._state is not ConnectionState.CONNECTED: return
        self._transition(ConnectionState.RECHECKING, reason); self._recheck_index = 0
        self._recheck_due_at = self._clock() + self._config.recheck_delays_seconds[0]
        print("connection state recheck scheduled", flush=True)

    def stop(self) -> None:
        if self._state is ConnectionState.STOPPED: return
        self._finish_connect_attempt()
        self._started = False; self._request_in_flight = False
        self._reconnect_due_at = self._recheck_due_at = self._login_deadline = None
        self._transition(ConnectionState.SHUTTING_DOWN, "connection manager shutting down")
        self._transition(ConnectionState.STOPPED, "connection manager stopped")

    def _begin_recheck(self) -> None:
        self._transition(ConnectionState.RECHECKING, "connection event/state mismatch detected")
        self._recheck_index = 0; self._recheck_due_at = self._clock() + self._config.recheck_delays_seconds[0]
        print("connection state recheck scheduled", flush=True)

    def _perform_recheck(self) -> None:
        state = self._read_connect_state("debounced connection state recheck")
        if state == 0:
            print("connection state recheck confirmed disconnected", flush=True)
            self._schedule_reconnect("connection state recheck confirmed disconnected"); return
        if state != 1: return
        self._recheck_index += 1
        if self._recheck_index < len(self._config.recheck_delays_seconds):
            previous_delay = self._config.recheck_delays_seconds[self._recheck_index - 1]
            next_delay = self._config.recheck_delays_seconds[self._recheck_index]
            self._recheck_due_at = self._clock() + max(0, next_delay - previous_delay)
            print("connection state recheck scheduled", flush=True); return
        print("connection state recheck confirmed connected", flush=True)
        self._mark_connected("connection state recheck confirmed connected")

    def _issue_initial_request(self) -> None:
        if self._request_in_flight or self._login_request_attempted: return
        self._login_request_attempted = True; self._request_in_flight = True
        self._login_deadline = self._clock() + self._config.login_timeout_seconds
        self._transition(ConnectionState.CONNECTING, "startup state was disconnected; initial request started")
        self._call_request_connect(initial=True)

    def _attempt_reconnect(self) -> None:
        if self._request_in_flight: return
        if self._reconnect_attempts >= self._config.max_reconnect_attempts:
            print("automatic reconnect exhausted", flush=True); self._fail("automatic reconnect exhausted"); return
        self._reconnect_attempts += 1; self._request_in_flight = True
        self._login_deadline = self._clock() + self._config.login_timeout_seconds
        self._transition(ConnectionState.RECONNECTING, f"automatic reconnect attempt: {self._reconnect_attempts}/{self._config.max_reconnect_attempts}")
        print(f"automatic reconnect attempt: {self._reconnect_attempts}/{self._config.max_reconnect_attempts}", flush=True)
        self._call_request_connect(initial=False)

    def _call_request_connect(self, *, initial: bool) -> None:
        try: result = int(self._connection.request_connect())
        except Exception as exc:
            self._schedule_reconnect(f"connection request raised {type(exc).__name__}"); return
        if result != 0: self._schedule_reconnect(f"connection request rejected: {result}")

    def _schedule_reconnect(self, reason: str) -> None:
        self._finish_connect_attempt()
        self._request_in_flight = False; self._login_deadline = self._recheck_due_at = None
        if self._reconnect_attempts >= self._config.max_reconnect_attempts:
            print("automatic reconnect exhausted", flush=True); self._fail("automatic reconnect exhausted"); return
        index = min(self._reconnect_attempts, len(self._config.reconnect_backoff_seconds) - 1)
        delay = self._config.reconnect_backoff_seconds[index]
        self._reconnect_due_at = self._clock() + delay
        self._transition(ConnectionState.RECONNECT_WAIT, reason)
        print(f"automatic reconnect scheduled in {delay:g} seconds", flush=True)

    def _fail(self, reason: str) -> None:
        self._finish_connect_attempt()
        self._request_in_flight = False; self._login_deadline = self._reconnect_due_at = self._recheck_due_at = None
        self._transition(ConnectionState.FAILED, reason)

    def _mark_connected(self, reason: str) -> None:
        self._finish_connect_attempt()
        recovered = self._reconnect_attempts > 0
        self._request_in_flight = False; self._reconnect_attempts = 0
        self._login_deadline = self._reconnect_due_at = self._recheck_due_at = None
        self._last_check_at = self._clock(); self._transition(ConnectionState.CONNECTED, reason)
        if recovered: print("automatic reconnect succeeded", flush=True)

    def _finish_connect_attempt(self) -> None:
        finish = getattr(self._connection, "finish_connect_attempt", None)
        if callable(finish):
            finish()

    def _read_connect_state(self, context: str) -> int | None:
        try: state = int(self._connection.get_connect_state())
        except Exception as exc:
            self._fail(f"{context} raised {type(exc).__name__}"); return None
        if state not in (0, 1): self._fail(f"{context} returned an invalid state"); return None
        return state

    def _transition(self, new_state: ConnectionState, reason: str) -> None:
        if new_state is self._state: return
        previous = self._state; self._state = new_state
        self._transitions.append(ConnectionTransition(previous, new_state, reason, self._reconnect_attempts))

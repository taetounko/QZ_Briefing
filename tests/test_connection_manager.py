"""Unit tests for the Qt-independent Kiwoom connection manager."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qz_briefing.kiwoom import (  # noqa: E402
    ConnectionConfig,
    ConnectionState,
    KiwoomConnectionManager,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeKiwoomConnection:
    def __init__(self, connect_state: int) -> None:
        self.connect_state = connect_state
        self.request_count = 0
        self.request_results: list[int] = []

    def get_connect_state(self) -> int:
        return self.connect_state

    def request_connect(self) -> int:
        self.request_count += 1
        if self.request_results:
            return self.request_results.pop(0)
        return 0


class KiwoomConnectionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.config = ConnectionConfig(
            check_interval_seconds=1,
            reconnect_delay_seconds=2,
            max_reconnect_attempts=3,
        )

    def make_manager(
        self, connection: FakeKiwoomConnection
    ) -> KiwoomConnectionManager:
        return KiwoomConnectionManager(connection, self.config, self.clock)

    def disconnect_connected_manager(
        self,
    ) -> tuple[FakeKiwoomConnection, KiwoomConnectionManager]:
        connection = FakeKiwoomConnection(1)
        manager = self.make_manager(connection)
        manager.start()
        connection.connect_state = 0
        self.clock.advance(1)
        manager.tick()
        return connection, manager

    def test_config_defaults(self) -> None:
        config = ConnectionConfig()
        self.assertEqual(config.check_interval_seconds, 30)
        self.assertEqual(config.reconnect_delay_seconds, 60)
        self.assertEqual(config.max_reconnect_attempts, 3)

    def test_start_when_already_connected(self) -> None:
        connection = FakeKiwoomConnection(1)
        manager = self.make_manager(connection)
        manager.start()
        self.assertEqual(manager.state, ConnectionState.CONNECTED)
        self.assertEqual(connection.request_count, 0)

    def test_disconnected_start_requests_connection_once(self) -> None:
        connection = FakeKiwoomConnection(0)
        manager = self.make_manager(connection)
        manager.start()
        self.assertEqual(manager.state, ConnectionState.CONNECTING)
        self.assertEqual(connection.request_count, 1)

    def test_connecting_ticks_do_not_duplicate_request(self) -> None:
        connection = FakeKiwoomConnection(0)
        manager = self.make_manager(connection)
        manager.start()
        self.clock.advance(100)
        manager.tick()
        manager.tick()
        self.assertEqual(connection.request_count, 1)
        self.assertEqual(manager.state, ConnectionState.CONNECTING)

    def test_login_success_requires_connected_state(self) -> None:
        connection = FakeKiwoomConnection(0)
        manager = self.make_manager(connection)
        manager.start()
        connection.connect_state = 1
        manager.handle_login_event(0)
        self.assertEqual(manager.state, ConnectionState.CONNECTED)
        self.assertEqual(manager.reconnect_attempts, 0)

    def test_login_error_schedules_reconnect(self) -> None:
        connection = FakeKiwoomConnection(0)
        manager = self.make_manager(connection)
        manager.start()
        manager.handle_login_event(-101)
        self.assertEqual(manager.state, ConnectionState.RECONNECT_WAIT)
        self.assertEqual(connection.request_count, 1)

    def test_connected_check_detects_disconnect(self) -> None:
        _, manager = self.disconnect_connected_manager()
        self.assertEqual(manager.state, ConnectionState.RECONNECT_WAIT)
        self.assertEqual(manager.reconnect_attempts, 0)

    def test_reconnect_is_not_requested_before_delay(self) -> None:
        connection, manager = self.disconnect_connected_manager()
        self.clock.advance(1.9)
        manager.tick()
        self.assertEqual(manager.state, ConnectionState.RECONNECT_WAIT)
        self.assertEqual(connection.request_count, 0)

    def test_reconnect_is_requested_after_delay(self) -> None:
        connection, manager = self.disconnect_connected_manager()
        self.clock.advance(2)
        manager.tick()
        self.assertEqual(manager.state, ConnectionState.CONNECTING)
        self.assertEqual(manager.reconnect_attempts, 1)
        self.assertEqual(connection.request_count, 1)

    def test_reconnect_success_resets_attempt_count(self) -> None:
        connection, manager = self.disconnect_connected_manager()
        self.clock.advance(2)
        manager.tick()
        connection.connect_state = 1
        manager.handle_login_event(0)
        self.assertEqual(manager.state, ConnectionState.CONNECTED)
        self.assertEqual(manager.reconnect_attempts, 0)

    def test_three_failed_reconnects_end_in_failed_state(self) -> None:
        connection, manager = self.disconnect_connected_manager()
        for attempt in range(1, 4):
            self.clock.advance(2)
            manager.tick()
            self.assertEqual(manager.reconnect_attempts, attempt)
            manager.handle_login_event(-101)

        self.assertEqual(manager.state, ConnectionState.FAILED)
        self.assertEqual(connection.request_count, 3)

    def test_stop_prevents_later_requests_and_events(self) -> None:
        connection = FakeKiwoomConnection(0)
        manager = self.make_manager(connection)
        manager.start()
        manager.stop()
        connection.connect_state = 1
        self.clock.advance(100)
        manager.tick()
        manager.handle_login_event(0)
        self.assertEqual(manager.state, ConnectionState.STOPPED)
        self.assertEqual(connection.request_count, 1)

    def test_invalid_connection_state_is_failed(self) -> None:
        connection = FakeKiwoomConnection(2)
        manager = self.make_manager(connection)
        manager.start()
        self.assertEqual(manager.state, ConnectionState.FAILED)
        self.assertEqual(connection.request_count, 0)

    def test_transition_history_contains_required_fields(self) -> None:
        _, manager = self.disconnect_connected_manager()
        transition = manager.transitions[-1]
        self.assertEqual(transition.previous_state, ConnectionState.CONNECTED)
        self.assertEqual(transition.new_state, ConnectionState.RECONNECT_WAIT)
        self.assertTrue(transition.reason)
        self.assertEqual(transition.reconnect_attempts, 0)

    def test_manager_does_not_accept_or_store_sensitive_information(self) -> None:
        parameters = set(inspect.signature(KiwoomConnectionManager).parameters)
        self.assertEqual(parameters, {"connection", "config", "clock"})

        manager = self.make_manager(FakeKiwoomConnection(1))
        stored_names = " ".join(vars(manager)).lower()
        for forbidden_name in (
            "password",
            "account",
            "credential",
            "certificate",
            "user_id",
            "pin",
        ):
            self.assertNotIn(forbidden_name, stored_names)


if __name__ == "__main__":
    unittest.main()

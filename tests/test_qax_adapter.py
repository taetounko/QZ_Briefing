"""Unit tests for the Kiwoom QAx adapter using fake widgets only."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qz_briefing.kiwoom import (  # noqa: E402
    ConnectionConfig,
    ConnectionState,
    KiwoomAdapterClosedError,
    KiwoomConnection,
    KiwoomConnectionManager,
    KiwoomConnectionRequestError,
    KiwoomConnectionStateError,
    KiwoomControlBindingError,
    KiwoomQAxAdapter,
)
from qz_briefing.kiwoom.qax_adapter import KIWOOM_CONTROL_ID  # noqa: E402
from qz_briefing.kiwoom import qax_adapter as qax_adapter_module  # noqa: E402


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []
        self.connect_count = 0
        self.disconnect_count = 0

    def connect(self, callback: object) -> None:
        self.connect_count += 1
        self.callbacks.append(callback)

    def disconnect(self, callback: object) -> None:
        self.disconnect_count += 1
        self.callbacks.remove(callback)

    def emit(self, *arguments: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*arguments)


class FakeQAxWidget:
    def __init__(
        self,
        *,
        set_control_result: bool = True,
        is_null: bool = False,
        connect_state: object = 0,
        request_result: object = 0,
    ) -> None:
        self.OnEventConnect = FakeSignal()
        self.OnReceiveTrData = FakeSignal()
        self.set_control_result = set_control_result
        self.is_null = is_null
        self.connect_state = connect_state
        self.request_result = request_result
        self.set_control_calls: list[str] = []
        self.dynamic_call_calls: list[str] = []
        self.master_names = {"005930": "삼성전자"}
        self.master_prices = {"005930": "+72,500"}
        self.login_info = {"ACCNO": "12345678;"}
        self.comm_data = {("OPT10001", "stock", 0, "현재가"): " +72,500 "}
        self.dynamic_call_arguments: list[tuple[object, ...]] = []
        self.close_count = 0
        self.delete_later_count = 0

    def setControl(self, control_id: str) -> bool:
        self.set_control_calls.append(control_id)
        return self.set_control_result

    def isNull(self) -> bool:
        return self.is_null

    def dynamicCall(self, signature: str, *arguments: object) -> object:
        self.dynamic_call_calls.append(signature)
        self.dynamic_call_arguments.append(arguments)
        if signature == "GetConnectState()":
            return self.connect_state
        if signature == "CommConnect()":
            return self.request_result
        if signature == "GetMasterCodeName(QString)":
            return self.master_names[str(arguments[0])]
        if signature == "GetMasterLastPrice(QString)":
            return self.master_prices[str(arguments[0])]
        if signature == "GetLoginInfo(QString)":
            return self.login_info[str(arguments[0])]
        if signature == "SetInputValue(QString, QString)":
            return None
        if signature == "CommRqData(QString, QString, int, QString)":
            return 0
        if signature == "GetCommData(QString, QString, int, QString)":
            return self.comm_data[tuple(arguments)]
        raise AssertionError(f"Unexpected dynamicCall signature: {signature}")

    def close(self) -> None:
        self.close_count += 1

    def deleteLater(self) -> None:
        self.delete_later_count += 1


class KiwoomQAxAdapterTests(unittest.TestCase):
    def test_get_login_info_uses_official_dynamic_call_without_transforming_value(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)

        self.assertEqual(adapter.get_login_info("ACCNO"), "12345678;")
        self.assertIn(("ACCNO",), widget.dynamic_call_arguments)

    def test_read_only_tr_wrappers_forward_arguments_and_trim_data(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        adapter.set_input_value("종목코드", "005930")
        result = adapter.request_tr("stock", "OPT10001", 0, "1000")
        value = adapter.get_comm_data("OPT10001", "stock", 0, "현재가")
        self.assertEqual(result, 0)
        self.assertEqual(value, "+72,500")
        self.assertEqual(
            widget.dynamic_call_arguments[-3:],
            [
                ("종목코드", "005930"),
                ("stock", "OPT10001", 0, "1000"),
                ("OPT10001", "stock", 0, "현재가"),
            ],
        )

    def test_read_only_master_data_wrappers_use_explicit_signatures(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        self.assertEqual(adapter.get_master_code_name("005930"), "삼성전자")
        self.assertEqual(adapter.get_master_last_price("005930"), "+72,500")
        self.assertEqual(
            widget.dynamic_call_calls,
            ["GetMasterCodeName(QString)", "GetMasterLastPrice(QString)"],
        )

    def test_default_factory_widget_is_not_rebound(self) -> None:
        widget = FakeQAxWidget()
        with patch.object(qax_adapter_module, "_create_qax_widget", return_value=widget):
            KiwoomQAxAdapter()
        self.assertEqual(widget.set_control_calls, [])

    def test_binds_default_control_id(self) -> None:
        widget = FakeQAxWidget()
        KiwoomQAxAdapter(widget=widget)
        self.assertEqual(widget.set_control_calls, [KIWOOM_CONTROL_ID])

    def test_widget_factory_can_be_injected(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget_factory=lambda: widget)
        self.assertFalse(adapter.closed)
        self.assertEqual(widget.set_control_calls, [KIWOOM_CONTROL_ID])

    def test_set_control_failure_raises(self) -> None:
        widget = FakeQAxWidget(set_control_result=False)
        with self.assertRaises(KiwoomControlBindingError):
            KiwoomQAxAdapter(widget=widget)

    def test_null_widget_after_binding_raises(self) -> None:
        widget = FakeQAxWidget(is_null=True)
        with self.assertRaises(KiwoomControlBindingError):
            KiwoomQAxAdapter(widget=widget)

    def test_get_connect_state_returns_zero(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget(connect_state=0))
        self.assertEqual(adapter.get_connect_state(), 0)

    def test_get_connect_state_returns_one(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget(connect_state=1))
        self.assertEqual(adapter.get_connect_state(), 1)

    def test_invalid_connect_state_raises(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget(connect_state=2))
        with self.assertRaises(KiwoomConnectionStateError):
            adapter.get_connect_state()

    def test_request_connect_returns_immediate_result(self) -> None:
        widget = FakeQAxWidget(request_result=-1)
        adapter = KiwoomQAxAdapter(widget=widget)
        self.assertEqual(adapter.request_connect(), -1)
        self.assertEqual(widget.dynamic_call_calls, ["CommConnect()"])
        self.assertEqual(adapter.connect_request_count, 1)

    def test_second_request_connect_is_rejected_without_calling_ocx(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        adapter.request_connect()
        with self.assertRaises(KiwoomConnectionRequestError):
            adapter.request_connect()
        self.assertEqual(widget.dynamic_call_calls, ["CommConnect()"])

    def test_connection_diagnostics_track_state_and_login_event(self) -> None:
        widget = FakeQAxWidget(connect_state=0)
        adapter = KiwoomQAxAdapter(widget=widget)

        self.assertEqual(adapter.get_connect_state(), 0)
        widget.OnEventConnect.emit(-101)

        self.assertEqual(adapter.last_connect_state, 0)
        self.assertEqual(adapter.login_event_count, 1)
        self.assertEqual(adapter.last_login_error_code, -101)

    def test_signal_is_connected_once(self) -> None:
        widget = FakeQAxWidget()
        KiwoomQAxAdapter(widget=widget)
        self.assertEqual(widget.OnEventConnect.connect_count, 1)
        self.assertEqual(len(widget.OnEventConnect.callbacks), 1)

    def test_login_event_delivers_integer_error_code(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        received: list[int] = []
        adapter.add_login_event_listener(received.append)
        widget.OnEventConnect.emit("0")
        self.assertEqual(received, [0])

    def test_multiple_login_listeners_are_called(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        first: list[int] = []
        second: list[int] = []
        adapter.add_login_event_listener(first.append)
        adapter.add_login_event_listener(second.append)
        widget.OnEventConnect.emit(-101)
        self.assertEqual(first, [-101])
        self.assertEqual(second, [-101])

    def test_duplicate_listener_is_not_registered_twice(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        received: list[int] = []
        adapter.add_login_event_listener(received.append)
        adapter.add_login_event_listener(received.append)
        widget.OnEventConnect.emit(0)
        self.assertEqual(received, [0])

    def test_listener_error_does_not_block_later_listener(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        received: list[int] = []

        def failing_listener(error_code: int) -> None:
            raise RuntimeError("listener failed")

        adapter.add_login_event_listener(failing_listener)
        adapter.add_login_event_listener(received.append)
        widget.OnEventConnect.emit(0)
        self.assertEqual(received, [0])
        self.assertEqual(adapter.listener_error_count, 1)

    def test_request_connect_after_close_raises(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget())
        adapter.close()
        with self.assertRaises(KiwoomAdapterClosedError):
            adapter.request_connect()

    def test_close_is_idempotent(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        adapter.close()
        adapter.close()
        self.assertEqual(widget.close_count, 1)
        self.assertEqual(widget.delete_later_count, 1)

    def test_close_disconnects_signal_and_disposes_widget(self) -> None:
        widget = FakeQAxWidget()
        adapter = KiwoomQAxAdapter(widget=widget)
        adapter.close()
        self.assertEqual(widget.OnEventConnect.disconnect_count, 1)
        self.assertEqual(widget.OnEventConnect.callbacks, [])
        self.assertEqual(widget.close_count, 1)
        self.assertEqual(widget.delete_later_count, 1)

    def test_adapter_does_not_store_sensitive_information(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget())
        stored_names = " ".join(vars(adapter)).lower()
        for forbidden_name in (
            "password",
            "account",
            "credential",
            "certificate",
            "user_id",
            "pin",
        ):
            self.assertNotIn(forbidden_name, stored_names)

    def test_adapter_matches_connection_protocol(self) -> None:
        adapter = KiwoomQAxAdapter(widget=FakeQAxWidget())
        self.assertIsInstance(adapter, KiwoomConnection)

    def test_login_event_can_drive_connection_manager(self) -> None:
        widget = FakeQAxWidget(connect_state=0, request_result=0)
        adapter = KiwoomQAxAdapter(widget=widget)
        manager = KiwoomConnectionManager(
            adapter,
            ConnectionConfig(
                check_interval_seconds=0,
                reconnect_delay_seconds=0,
                max_reconnect_attempts=3,
            ),
            clock=lambda: 0.0,
        )
        adapter.add_login_event_listener(manager.handle_login_event)

        manager.start()
        self.assertEqual(manager.state, ConnectionState.CONNECTING)
        widget.connect_state = 1
        widget.OnEventConnect.emit(0)
        self.assertEqual(manager.state, ConnectionState.CONNECTED)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from qz_briefing.__main__ import parse_cli_arguments
from qz_briefing.kiwoom.login_diagnostic import diagnose_login_core, probe_active_x


class Adapter:
    def __init__(self, *, event_code=0, event_state=1, request_result=0, emit=True):
        self.event_code = event_code
        self.state = 0
        self.event_state = event_state
        self.request_result = request_result
        self.emit = emit
        self.listener = None
        self.calls = []

    def add_login_event_listener(self, listener):
        self.calls.append("connect_event")
        self.listener = listener

    def get_connect_state(self):
        self.calls.append("get_state")
        return self.state

    def request_connect(self):
        self.calls.append("comm_connect")
        return self.request_result


class Waiter:
    def __init__(self, adapter, *, timeout=False, timeout_state=0):
        self.adapter = adapter
        self.timed_out = timeout
        self.timeout_state = timeout_state
        self.entered = False
        self.quit_count = 0

    def wait(self):
        self.entered = True
        if self.adapter.emit:
            self.adapter.state = self.adapter.event_state
            self.adapter.listener(self.adapter.event_code)
        else:
            self.adapter.state = self.timeout_state

    def quit(self):
        self.quit_count += 1


def test_event_is_connected_before_comm_connect_and_loop_is_entered():
    adapter = Adapter()
    waiter = Waiter(adapter)
    result = diagnose_login_core(adapter, waiter)
    assert adapter.calls.index("connect_event") < adapter.calls.index("comm_connect")
    assert result["event_loop_entered"] and result["status"] == "LOGIN_SUCCESS"
    assert waiter.quit_count == 1


def test_failed_login_event_is_not_success():
    adapter = Adapter(event_code=-101, event_state=0)
    result = diagnose_login_core(adapter, Waiter(adapter))
    assert result["status"] == "DISCONNECTED_AFTER_LOGIN_EVENT"
    assert result["event"]["error_code"] == -101


def test_timeout_rechecks_connection_and_distinguishes_no_event():
    adapter = Adapter(emit=False)
    result = diagnose_login_core(adapter, Waiter(adapter, timeout=True))
    assert result["timeout"]
    assert result["status"] == "TIMEOUT_NO_LOGIN_EVENT"
    assert result["final_connect_state"] == 0


def test_timeout_with_connected_state_is_success_without_guessing_event_code():
    adapter = Adapter(emit=False)
    result = diagnose_login_core(adapter, Waiter(adapter, timeout=True, timeout_state=1))
    assert result["status"] == "LOGIN_SUCCESS"
    assert not result["event_received"]


def test_comm_connect_failure_does_not_enter_event_loop_or_retry():
    adapter = Adapter(request_result=-100)
    waiter = Waiter(adapter)
    result = diagnose_login_core(adapter, waiter)
    assert result["status"] == "COMM_CONNECT_CALL_FAILED"
    assert not waiter.entered
    assert adapter.calls.count("comm_connect") == 1


class Widget:
    comm_connect_calls = 0

    def __init__(self, control=None, *, valid=True):
        self.valid = valid
        self.control = control

    def setControl(self, control):
        self.control = control
        return self.valid

    def isNull(self):
        return not self.valid

    def dynamicCall(self, *args):
        if args and args[0] == "CommConnect()":
            Widget.comm_connect_calls += 1

    def close(self):
        pass

    def deleteLater(self):
        pass


def test_active_x_probe_compares_both_forms_without_comm_connect():
    Widget.comm_connect_calls = 0
    result = probe_active_x(Widget)
    assert result["constructor_control"]["created"]
    assert result["blank_then_set_control"]["set_control_result"]
    assert Widget.comm_connect_calls == 0


def test_invalid_active_x_is_reported_without_any_login_call():
    result = probe_active_x(lambda control=None: Widget(control, valid=False))
    assert result["constructor_control"]["is_null"]
    assert not result["blank_then_set_control"]["set_control_result"]


def test_login_diagnostic_cli_option_is_available():
    assert parse_cli_arguments(["--diagnose-kiwoom-login"]).diagnose_kiwoom_login

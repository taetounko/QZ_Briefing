"""Sequential Kiwoom TR queue tests with no real OCX calls."""

import pytest

from qz_briefing.kiwoom import (
    KiwoomTrClosedError,
    KiwoomTrRequestQueue,
    KiwoomTrTimeoutError,
    TrRequest,
)


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()  # type: ignore[operator]


class FakeTimer:
    def __init__(self) -> None:
        self.timeout = FakeSignal()
        self.single_shot = False
        self.started_with: int | None = None
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int) -> None:
        self.started_with = milliseconds

    def stop(self) -> None:
        self.stop_count += 1


class FakeTrAdapter:
    def __init__(self) -> None:
        self.listener: object | None = None
        self.inputs: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, int, str]] = []
        self.values: dict[tuple[str, str, int, str], str] = {}
        self.repeat_count = 0

    def add_tr_data_listener(self, callback: object) -> None:
        self.listener = callback

    def set_input_value(self, item: str, value: str) -> None:
        self.inputs.append((item, value))

    def request_tr(
        self, request_name: str, tr_code: str, previous_next: int, screen_no: str
    ) -> int:
        self.requests.append((request_name, tr_code, previous_next, screen_no))
        return 0

    def get_comm_data(
        self, tr_code: str, request_name: str, index: int, item_name: str
    ) -> str:
        return self.values[(tr_code, request_name, index, item_name)]

    def get_repeat_count(self, tr_code: str, request_name: str) -> int:
        return self.repeat_count

    def respond(self, request_index: int = -1) -> None:
        request_name, tr_code, _, screen_no = self.requests[request_index]
        self.listener(screen_no, request_name, tr_code, "주식기본정보")  # type: ignore[operator]


def request(name: str = "stock") -> TrRequest:
    return TrRequest(
        request_name=name,
        tr_code="OPT10001",
        inputs={"종목코드": "005930"},
        output_fields=("현재가", "등락율"),
        timeout_ms=3210,
    )


def make_queue():
    adapter = FakeTrAdapter()
    timers: list[FakeTimer] = []

    def timer_factory() -> FakeTimer:
        timer = FakeTimer()
        timers.append(timer)
        return timer

    queue = KiwoomTrRequestQueue(adapter, timer_factory=timer_factory)
    return queue, adapter, timers


def test_request_sets_inputs_and_calls_comm_rq_data() -> None:
    queue, adapter, timers = make_queue()
    queue.submit(request(), lambda data: None, lambda error: None)
    assert adapter.inputs == [("종목코드", "005930")]
    assert adapter.requests == [("stock", "OPT10001", 0, "1000")]
    assert timers[0].single_shot
    assert timers[0].started_with == 3210


def test_matching_response_parses_requested_fields() -> None:
    queue, adapter, _ = make_queue()
    adapter.values = {
        ("OPT10001", "stock", 0, "현재가"): "+72,500",
        ("OPT10001", "stock", 0, "등락율"): "+1.25",
    }
    results: list[dict[str, str]] = []
    queue.submit(request(), results.append, lambda error: None)
    adapter.respond()
    assert results == [{"현재가": "+72,500", "등락율": "+1.25"}]


def test_timeout_completes_request_with_error() -> None:
    queue, _, timers = make_queue()
    errors: list[Exception] = []
    queue.submit(request(), lambda data: None, errors.append)
    timers[0].timeout.emit()
    assert len(errors) == 1
    assert isinstance(errors[0], KiwoomTrTimeoutError)
    assert queue.pending_count == 0


def test_repeated_response_does_not_complete_twice() -> None:
    queue, adapter, _ = make_queue()
    adapter.values = {
        ("OPT10001", "stock", 0, "현재가"): "72500",
        ("OPT10001", "stock", 0, "등락율"): "1.25",
    }
    results: list[dict[str, str]] = []
    queue.submit(request(), results.append, lambda error: None)
    adapter.respond()
    adapter.respond()
    assert len(results) == 1


def test_requests_are_sent_sequentially_without_screen_collision() -> None:
    queue, adapter, _ = make_queue()
    for name in ("first", "second"):
        adapter.values[("OPT10001", name, 0, "현재가")] = "72500"
        adapter.values[("OPT10001", name, 0, "등락율")] = "1.25"
    results: list[str] = []
    queue.submit(request("first"), lambda data: results.append("first"), lambda e: None)
    queue.submit(request("second"), lambda data: results.append("second"), lambda e: None)
    assert [item[0] for item in adapter.requests] == ["first"]
    adapter.respond(0)
    assert [item[0] for item in adapter.requests] == ["first", "second"]
    assert adapter.requests[0][3] == "1000"
    assert adapter.requests[1][3] == "1001"
    adapter.respond(1)
    assert results == ["first", "second"]


def test_stale_timeout_cannot_fail_the_next_request() -> None:
    queue, adapter, timers = make_queue()
    for name in ("first", "second"):
        adapter.values[("OPT10001", name, 0, "현재가")] = "72500"
        adapter.values[("OPT10001", name, 0, "등락율")] = "1.25"
    errors: list[Exception] = []
    results: list[str] = []
    queue.submit(request("first"), lambda data: results.append("first"), errors.append)
    queue.submit(request("second"), lambda data: results.append("second"), errors.append)
    adapter.respond(0)
    timers[0].timeout.emit()
    adapter.respond(1)
    assert errors == []
    assert results == ["first", "second"]


def test_close_cancels_active_and_pending_requests() -> None:
    queue, _, _ = make_queue()
    errors: list[Exception] = []
    queue.submit(request("first"), lambda data: None, errors.append)
    queue.submit(request("second"), lambda data: None, errors.append)
    queue.close()
    assert len(errors) == 2
    assert all(isinstance(error, KiwoomTrClosedError) for error in errors)
    assert queue.pending_count == 0


def test_closed_queue_rejects_new_requests() -> None:
    queue, _, _ = make_queue()
    queue.close()
    with pytest.raises(KiwoomTrClosedError):
        queue.submit(request(), lambda data: None, lambda error: None)


def test_repeated_response_parses_every_official_output_row() -> None:
    queue, adapter, _ = make_queue()
    repeated = TrRequest(
        request_name="flows",
        tr_code="OPT10051",
        inputs={"시장구분": "0"},
        output_fields=("업종코드", "개인순매수"),
        repeat=True,
    )
    adapter.repeat_count = 2
    adapter.values = {
        ("OPT10051", "flows", 0, "업종코드"): "001",
        ("OPT10051", "flows", 0, "개인순매수"): "-1,000",
        ("OPT10051", "flows", 1, "업종코드"): "002",
        ("OPT10051", "flows", 1, "개인순매수"): "2,000",
    }
    results: list[object] = []
    queue.submit(repeated, results.append, lambda error: None)
    adapter.respond()
    assert results == [
        [
            {"업종코드": "001", "개인순매수": "-1,000"},
            {"업종코드": "002", "개인순매수": "2,000"},
        ]
    ]

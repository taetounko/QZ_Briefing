from datetime import datetime

from qz_briefing.briefing.preopen_monitoring import (
    KiwoomPreopenRealSource, PreopenMonitoringController, empty_result,
    prioritize_codes, sample_times,
)


class Signal:
    def __init__(self): self.callback = None
    def connect(self, callback): self.callback = callback


class Timer:
    def __init__(self): self.timeout, self.delay, self.stopped = Signal(), None, False
    def setSingleShot(self, value): assert value
    def start(self, milliseconds): self.delay = milliseconds
    def stop(self): self.stopped = True


def test_sampling_includes_8_00_8_55_and_8_59():
    values = [item.strftime("%H:%M") for item in sample_times(datetime(2026, 7, 22, 7, 30))]
    assert values[0] == "08:00"
    assert "08:55" in values
    assert values[-1] == "08:59"


def test_late_start_is_partial_and_samples_immediately():
    timers = []
    now = datetime(2026, 7, 22, 8, 30)
    controller = PreopenMonitoringController(
        lambda: {"market_open_detected": False}, clock=lambda: now,
        timer_factory=lambda: timers.append(Timer()) or timers[-1],
    )
    controller.start()
    assert controller.result["coverage_status"] == "partial"
    assert controller.result["sample_count"] == 1
    assert "partial_preopen_window" in controller.result["warnings"]


def test_shutdown_stops_timer_and_no_more_samples():
    timers = []
    controller = PreopenMonitoringController(
        lambda: {}, clock=lambda: datetime(2026, 7, 22, 7, 30),
        timer_factory=lambda: timers.append(Timer()) or timers[-1],
    )
    controller.start(); controller.stop()
    assert timers[0].stopped


def test_targets_are_prioritized_and_deduplicated():
    assert prioritize_codes(["123456", "005930"], ["234567"], ["123456"]) == (
        "005930", "000660", "123456", "234567"
    )


def test_unavailable_result_does_not_invent_investor_flow():
    result = empty_result(datetime(2026, 7, 22, 9, 10))
    assert result["flow_availability"]["foreign_flow"] == "not_available"


def test_actual_stock_trade_confirms_market_open():
    class Adapter:
        def add_real_data_listener(self, callback): self.callback = callback
        def get_comm_real_data(self, code, fid): return ""
    adapter = Adapter()
    source = KiwoomPreopenRealSource(adapter, ["005930"])
    adapter.callback("005930", "주식체결", "")
    assert source.snapshot()["market_open_detected"] is True


def test_market_state_refresh_does_not_add_sample():
    controller = PreopenMonitoringController(
        lambda: {"market_open_detected": True},
        clock=lambda: datetime(2026, 7, 22, 9, 0), timer_factory=Timer,
    )
    assert controller.refresh_market_state()
    assert controller.result["sample_count"] == 0

from __future__ import annotations

import time

from qz_briefing.kiwoom.connection_confirmation import KiwoomConnectionConfirmationMonitor, is_safe_confirmation


def test_only_exact_openapi_confirmation_shape_is_allowed():
    assert is_safe_confirmation(title="KHOpenAPI 접속 확인", window_class="#32770", button_text="확인", body_texts=("API 접속을 확인합니다",))


def test_wrong_class_title_or_button_is_rejected():
    common = dict(title="KHOpenAPI 접속 확인", window_class="#32770", button_text="확인", body_texts=("API 접속을 확인합니다",))
    assert not is_safe_confirmation(**{**common, "window_class": "Chrome_WidgetWin_1"})
    assert not is_safe_confirmation(**{**common, "title": "다른 프로그램"})
    assert not is_safe_confirmation(**{**common, "button_text": "예"})


def test_order_certificate_account_password_and_security_dialogs_are_never_allowed():
    for forbidden in ("주문", "인증서", "계좌비밀번호", "보안경고"):
        assert not is_safe_confirmation(title="KHOpenAPI 접속 확인", window_class="#32770", button_text="확인", body_texts=(f"API 접속 {forbidden}",))


def test_monitor_is_nonblocking_and_stops_after_safe_click():
    calls = []
    monitor = KiwoomConnectionConfirmationMonitor(poll_seconds=0.001, max_seconds=1, scanner=lambda: (calls.append(1) or (True, True, False)))
    started = time.monotonic()
    monitor.start()
    assert time.monotonic() - started < 0.1
    for _ in range(100):
        if not monitor.running:
            break
        time.sleep(0.001)
    assert calls and not monitor.running


def test_monitor_failure_does_not_block_and_can_be_cleaned_up():
    monitor = KiwoomConnectionConfirmationMonitor(poll_seconds=0.001, max_seconds=1, scanner=lambda: (_ for _ in ()).throw(RuntimeError("scan")))
    monitor.start()
    monitor.stop()
    assert not monitor.running

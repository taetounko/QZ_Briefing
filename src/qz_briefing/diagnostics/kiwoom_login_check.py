"""Verify one Kiwoom OpenAPI+ login attempt and its connection event."""

from __future__ import annotations

import os
import platform
import struct
import sys
import traceback
from pathlib import Path
from typing import TextIO


CONTROL_ID = "KHOPENAPI.KHOpenAPICtrl.1"
LOGIN_TIMEOUT_MS = 300_000
LOGIN_ERROR_DESCRIPTIONS = {
    0: "SUCCESS",
    -100: "USER_INFO_EXCHANGE_FAILED",
    -101: "SERVER_CONNECTION_FAILED",
    -102: "VERSION_PROCESSING_FAILED",
}

_log_stream: TextIO | None = None


def _open_log() -> None:
    global _log_stream
    project_root = Path(__file__).resolve().parents[3]
    log_path = project_root / "logs" / "kiwoom_login_check.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_stream = log_path.open("w", encoding="utf-8", newline="\n")


def _close_log() -> None:
    global _log_stream
    if _log_stream is not None:
        _log_stream.close()
        _log_stream = None


def _emit(message: str, *, error: bool = False) -> None:
    global _log_stream
    output = sys.stderr if error else sys.stdout
    print(message, file=output, flush=True)
    if _log_stream is not None:
        try:
            _log_stream.write(f"{message}\n")
            _log_stream.flush()
        except BaseException as exc:
            print(
                f"LOG_WRITE_ERROR: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            _log_stream = None


def _record_error(
    errors: list[tuple[str, str]], exc: BaseException, *, context: str
) -> None:
    error_type = type(exc).__name__
    error_message = str(exc)
    errors.append((error_type, error_message))
    _emit(f"{context}: {error_type}: {error_message}", error=True)


def _unhandled_exception_hook(
    exception_type: type[BaseException],
    exception: BaseException,
    exception_traceback: object,
) -> None:
    _emit(
        f"UNHANDLED_EXCEPTION: {exception_type.__name__}: {exception}", error=True
    )
    formatted_traceback = "".join(
        traceback.format_exception(
            exception_type, exception, exception_traceback
        )
    ).rstrip()
    if formatted_traceback:
        _emit(formatted_traceback, error=True)


def _read_connection_state(qax_widget: object) -> int:
    raw_state = qax_widget.dynamicCall("GetConnectState()")
    if raw_state is None:
        raise RuntimeError("GetConnectState returned None")
    try:
        state = int(raw_state)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"GetConnectState returned a non-integer value: {raw_state!r}"
        ) from exc
    if state not in (0, 1):
        raise RuntimeError(f"GetConnectState returned an unexpected value: {state}")
    return state


class LoginEventWaiter:
    """Store login-event state and stop only the dedicated login loop."""

    def __init__(
        self, login_event_loop: object, errors: list[tuple[str, str]]
    ) -> None:
        self.login_event_loop = login_event_loop
        self.errors = errors
        self.event_received = False
        self.timed_out = False
        self.error_code: int | None = None

    def on_event_connect(self, error_code: int) -> None:
        _emit(f"OnEventConnect ENTER error code: {error_code}")
        try:
            self.error_code = int(error_code)
            self.event_received = True
        except BaseException as exc:
            _record_error(self.errors, exc, context="OnEventConnect error")
        finally:
            if self.login_event_loop.isRunning():
                self.login_event_loop.quit()

    def on_timeout(self) -> None:
        self.timed_out = True
        _emit("Login timeout: 300 seconds")
        if self.login_event_loop.isRunning():
            self.login_event_loop.quit()


def _connection_description(state: int | None) -> str:
    return {0: "NOT_CONNECTED", 1: "CONNECTED"}.get(state, "UNKNOWN")


def _emit_final_connection_state(state: int | None) -> None:
    _emit(
        "Final connection state: "
        f"{state if state is not None else 'UNKNOWN'}"
    )
    _emit(
        "Final connection description: "
        f"{_connection_description(state)}"
    )


def _emit_result(
    *,
    python_version: str,
    architecture_bits: int,
    platform_name: str,
    control_binding: bool,
    initial_connection_state: int | None,
    login_request: str,
    event_received: bool,
    login_error_code: int | None,
    login_result: str,
    final_connection_state: int | None,
    overall_ok: bool,
    final_state_already_emitted: bool = False,
) -> None:
    if not final_state_already_emitted:
        _emit_final_connection_state(final_connection_state)
    _emit(f"Python: {python_version}")
    _emit(f"Architecture: {architecture_bits}-bit")
    _emit(f"Platform: {platform_name}")
    _emit(f"Kiwoom control binding: {'PASS' if control_binding else 'FAIL'}")
    _emit(
        "Initial connection state: "
        f"{initial_connection_state if initial_connection_state is not None else 'UNKNOWN'}"
    )
    _emit(f"Login request: {login_request}")
    if login_request == "SKIPPED":
        _emit("OnEventConnect received: SKIPPED")
    else:
        _emit(f"OnEventConnect received: {'PASS' if event_received else 'FAIL'}")
    _emit(
        "Login error code: "
        f"{login_error_code if login_error_code is not None else 'NOT_APPLICABLE'}"
    )
    login_error_description = (
        LOGIN_ERROR_DESCRIPTIONS.get(login_error_code, "UNKNOWN_ERROR")
        if login_error_code is not None
        else "NOT_APPLICABLE"
    )
    _emit(f"Login error description: {login_error_description}")
    _emit(f"Login result: {login_result}")
    _emit(f"Overall: {'PASS' if overall_ok else 'FAIL'}")


def main() -> int:
    """Run a single login check with a dedicated bounded event loop."""
    _open_log()

    python_version = platform.python_version()
    architecture_bits = struct.calcsize("P") * 8
    platform_name = platform.system()

    _emit("SCRIPT_START")
    _emit(f"Python: {python_version}")
    _emit(f"Executable: {sys.executable}")
    _emit(f"Process ID: {os.getpid()}")

    application = None
    qax_widget = None
    login_event_loop = None
    timeout_timer = None
    login_waiter: LoginEventWaiter | None = None
    owns_application = False
    previous_quit_on_last_window_closed: bool | None = None
    signal_connected = False
    control_binding = False
    initial_connection_state: int | None = None
    final_connection_state: int | None = None
    login_request = "NOT_ATTEMPTED"
    login_result = "NOT_RUN"
    overall_ok = False
    result_emitted = False
    errors: list[tuple[str, str]] = []

    try:
        if platform_name != "Windows":
            raise RuntimeError(f"Windows is required, detected {platform_name}")
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError(f"Python 3.11 is required, detected {python_version}")
        if architecture_bits != 32:
            raise RuntimeError(
                f"32-bit Python is required, detected {architecture_bits}-bit"
            )

        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtCore import QEventLoop, QTimer
        from PyQt5.QtWidgets import QApplication

        _emit("QApplication creation: BEFORE")
        application = QApplication.instance()
        if application is None:
            application = QApplication([])
            owns_application = True
        _emit("QApplication creation: AFTER")
        previous_quit_on_last_window_closed = bool(
            application.quitOnLastWindowClosed()
        )
        application.setQuitOnLastWindowClosed(False)
        _emit("QApplication quitOnLastWindowClosed: False")

        def on_about_to_quit() -> None:
            _emit("QApplication aboutToQuit")

        application.aboutToQuit.connect(on_about_to_quit)

        _emit("QAxWidget creation: BEFORE")
        qax_widget = QAxWidget()
        _emit("QAxWidget creation: AFTER")
        binding_result = bool(qax_widget.setControl(CONTROL_ID))
        widget_is_null = bool(qax_widget.isNull())
        _emit(f"setControl result: {binding_result}")
        _emit(f"QAxWidget isNull: {widget_is_null}")
        control_binding = binding_result and not widget_is_null
        if not control_binding:
            raise RuntimeError("Kiwoom OpenAPI+ control binding failed")

        login_event_loop = QEventLoop()
        login_waiter = LoginEventWaiter(login_event_loop, errors)
        qax_widget.OnEventConnect.connect(login_waiter.on_event_connect)
        signal_connected = True

        initial_connection_state = _read_connection_state(qax_widget)
        _emit(f"Initial GetConnectState result: {initial_connection_state}")

        if initial_connection_state == 1:
            login_request = "SKIPPED"
        else:
            login_request = "SENT"
            _emit("CommConnect call: BEFORE")
            raw_request_result = qax_widget.dynamicCall("CommConnect()")
            _emit("CommConnect call: AFTER")
            _emit(f"CommConnect immediate return: {raw_request_result}")
            if raw_request_result is None:
                raise RuntimeError("Login request returned None")
            try:
                request_result = int(raw_request_result)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Login request returned a non-integer value: {raw_request_result!r}"
                ) from exc
            if request_result != 0:
                raise RuntimeError(f"Login request failed with code {request_result}")

            if not login_waiter.event_received:
                timeout_timer = QTimer()
                timeout_timer.setSingleShot(True)
                timeout_timer.timeout.connect(login_waiter.on_timeout)
                timeout_timer.start(LOGIN_TIMEOUT_MS)
                _emit("Qt event loop: BEFORE_ENTER")
                login_event_loop.exec_()
                _emit("Qt event loop: AFTER_EXIT")
                timeout_timer.stop()

        application.processEvents()
        _emit("QApplication.processEvents: AFTER_LOGIN_LOOP")
        final_connection_state = _read_connection_state(qax_widget)
        _emit_final_connection_state(final_connection_state)

        if initial_connection_state == 1:
            login_result = (
                "ALREADY_CONNECTED"
                if final_connection_state == 1
                else "FINAL_STATE_NOT_CONNECTED"
            )
        elif login_waiter.timed_out:
            login_result = "TIMEOUT"
        elif not login_waiter.event_received:
            login_result = "UNKNOWN_ERROR"
        elif login_waiter.error_code == 0:
            login_result = (
                "SUCCESS"
                if final_connection_state == 1
                else "FINAL_STATE_NOT_CONNECTED"
            )
        else:
            login_result = LOGIN_ERROR_DESCRIPTIONS.get(
                login_waiter.error_code, "UNKNOWN_ERROR"
            )

        overall_ok = (
            control_binding
            and login_result in {"SUCCESS", "ALREADY_CONNECTED"}
            and final_connection_state == 1
            and not errors
        )
        _emit_result(
            python_version=python_version,
            architecture_bits=architecture_bits,
            platform_name=platform_name,
            control_binding=control_binding,
            initial_connection_state=initial_connection_state,
            login_request=login_request,
            event_received=login_waiter.event_received,
            login_error_code=login_waiter.error_code,
            login_result=login_result,
            final_connection_state=final_connection_state,
            overall_ok=overall_ok,
            final_state_already_emitted=True,
        )
        result_emitted = True
    except BaseException as exc:
        _record_error(errors, exc, context="MAIN_EXCEPTION")
        login_result = "ERROR"
        if not result_emitted:
            _emit_result(
                python_version=python_version,
                architecture_bits=architecture_bits,
                platform_name=platform_name,
                control_binding=control_binding,
                initial_connection_state=initial_connection_state,
                login_request=login_request,
                event_received=(
                    login_waiter.event_received if login_waiter is not None else False
                ),
                login_error_code=(
                    login_waiter.error_code if login_waiter is not None else None
                ),
                login_result=login_result,
                final_connection_state=final_connection_state,
                overall_ok=False,
            )
            result_emitted = True
    finally:
        if timeout_timer is not None:
            try:
                timeout_timer.stop()
                timeout_timer.deleteLater()
            except BaseException as exc:
                _record_error(errors, exc, context="TIMER_CLEANUP_EXCEPTION")

        if login_event_loop is not None and login_event_loop.isRunning():
            login_event_loop.quit()

        if qax_widget is not None:
            try:
                if signal_connected and login_waiter is not None:
                    qax_widget.OnEventConnect.disconnect(
                        login_waiter.on_event_connect
                    )
                qax_widget.close()
                qax_widget.deleteLater()
            except BaseException as exc:
                _record_error(errors, exc, context="QAX_CLEANUP_EXCEPTION")

        if application is not None:
            try:
                application.processEvents()
                if previous_quit_on_last_window_closed is not None:
                    application.setQuitOnLastWindowClosed(
                        previous_quit_on_last_window_closed
                    )
                if owns_application:
                    application.quit()
            except BaseException as exc:
                _record_error(errors, exc, context="QAPPLICATION_CLEANUP_EXCEPTION")

        timeout_timer = None
        login_event_loop = None
        login_waiter = None
        qax_widget = None
        application = None
        _close_log()

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.excepthook = _unhandled_exception_hook
    sys.exit(main())

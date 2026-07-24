"""Login-only Kiwoom ActiveX diagnostic with no data or account requests."""

from __future__ import annotations

import csv
import ctypes
import io
import os
import platform
import subprocess
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from .qax_adapter import KIWOOM_CONTROL_ID, KiwoomQAxAdapter


LOGIN_ERRORS = {0: "success", -100: "user information exchange failed", -101: "server connection failed", -102: "version processing failed"}


class QtLoginWaiter:
    """Own the nested Qt loop and timers for their complete callback lifetime."""

    def __init__(self, timeout_ms: int = 180_000) -> None:
        from PyQt5.QtCore import QEventLoop, QTimer

        self.loop = QEventLoop()
        self.timeout = QTimer()
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._on_timeout)
        self.progress = QTimer()
        self.progress.timeout.connect(lambda: print("LOGIN_WAITING elapsed_increment=30s", flush=True))
        self.timeout_ms = timeout_ms
        self.timed_out = False
        self.entered = False
        self.quit_count = 0

    def _on_timeout(self) -> None:
        self.timed_out = True
        self.quit()

    def quit(self) -> None:
        self.quit_count += 1
        self.loop.quit()

    def wait(self) -> None:
        self.entered = True
        self.progress.start(30_000)
        self.timeout.start(self.timeout_ms)
        self.loop.exec_()
        self.timeout.stop()
        self.progress.stop()


def diagnose_login_core(adapter: Any, waiter: Any) -> dict[str, object]:
    """Connect the event first, issue one CommConnect, and await a final state."""
    result: dict[str, object] = {
        "event_connected": False,
        "event_received": False,
        "comm_connect_calls": 0,
        "timeout": False,
    }
    events: list[dict[str, object]] = []

    def on_event(error_code: int) -> None:
        events.append({
            "received_at": datetime.now().isoformat(),
            "error_code": int(error_code),
            "meaning": LOGIN_ERRORS.get(int(error_code), "unknown login error"),
            "main_qt_thread": _is_main_qt_thread(),
            "connect_state": adapter.get_connect_state(),
        })
        waiter.quit()

    # Keep a strong reference in this frame through waiter.wait().
    result["event_connected_at"] = datetime.now().isoformat()
    adapter.add_login_event_listener(on_event)
    result["event_connected"] = True
    result["callback_alive"] = callable(on_event)
    result["connect_state_before"] = adapter.get_connect_state()
    try:
        result["comm_connect_calls"] = 1
        result["comm_connect_return_code"] = adapter.request_connect()
    except Exception as exc:
        result.update({"status": "COMM_CONNECT_CALL_FAILED", "exception": f"{type(exc).__name__}: {exc}"})
        return result
    result["connect_state_immediately_after"] = adapter.get_connect_state()
    if result["comm_connect_return_code"] != 0:
        result["status"] = "COMM_CONNECT_CALL_FAILED"
        return result
    waiter.wait()
    finish = getattr(adapter, "finish_connect_attempt", None)
    if callable(finish):
        finish()
    result["event_loop_entered"] = bool(waiter.entered)
    result["timeout"] = bool(waiter.timed_out)
    result["event_received"] = bool(events)
    result["event"] = events[0] if events else None
    result["final_connect_state"] = adapter.get_connect_state()
    if events:
        result["status"] = "LOGIN_SUCCESS" if events[0]["error_code"] == 0 and result["final_connect_state"] == 1 else "DISCONNECTED_AFTER_LOGIN_EVENT"
    elif result["final_connect_state"] == 1:
        result["status"] = "LOGIN_SUCCESS"
    else:
        result["status"] = "TIMEOUT_NO_LOGIN_EVENT"
    return result


def _is_main_qt_thread() -> bool:
    try:
        from PyQt5.QtCore import QCoreApplication, QThread
        app = QCoreApplication.instance()
        return bool(app and QThread.currentThread() == app.thread())
    except Exception:
        return False


def probe_active_x(widget_factory: Callable[..., Any]) -> dict[str, object]:
    """Compare both construction forms without invoking CommConnect."""
    probes: dict[str, object] = {}
    for label, direct in (("constructor_control", True), ("blank_then_set_control", False)):
        widget = None
        try:
            widget = widget_factory(KIWOOM_CONTROL_ID) if direct else widget_factory()
            set_result = None if direct else bool(widget.setControl(KIWOOM_CONTROL_ID))
            probes[label] = {
                "created": True,
                "set_control_result": set_result,
                "is_null": bool(widget.isNull()),
                "dynamic_call_available": callable(getattr(widget, "dynamicCall", None)),
                "exception": None,
            }
        except Exception as exc:
            probes[label] = {"created": False, "set_control_result": False, "is_null": True, "dynamic_call_available": False, "exception": f"{type(exc).__name__}: {exc}"}
        finally:
            if widget is not None:
                try:
                    widget.close()
                    widget.deleteLater()
                except Exception:
                    pass
    return probes


def process_inventory() -> dict[str, int]:
    """Return only sanitized executable-name counts; never command lines or titles."""
    try:
        completed = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, encoding="mbcs", errors="replace", check=False)
        names = [row[0].lower() for row in csv.reader(io.StringIO(completed.stdout)) if row]
    except Exception:
        names = []
    groups = {
        "kiwoom": lambda name: any(token in name for token in ("khmini", "nkmini", "opstarter", "khopenapi", "hero")),
        "koa_studio": lambda name: "koa" in name,
        "python": lambda name: name.startswith("python"),
        "qz_briefing": lambda name: "qz_briefing" in name,
    }
    return {group: sum(predicate(name) for name in names) for group, predicate in groups.items()}


def environment_diagnostic() -> dict[str, object]:
    session_name = os.environ.get("SESSIONNAME", "")
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    return {
        "executable": sys.executable,
        "python": sys.version,
        "bits": 64 if sys.maxsize > 2**32 else 32,
        "user_present": bool(os.environ.get("USERNAME")),
        "pid": os.getpid(),
        "administrator": admin,
        "privilege_mismatch": "not_determined_without_inspecting_other_process_tokens",
        "interactive_desktop": bool(session_name and session_name.lower() not in {"services", "service"}),
        "session_kind": "interactive" if session_name and session_name.lower() not in {"services", "service"} else "unknown_or_noninteractive",
        "platform": platform.platform(),
    }


def run_login_diagnostic(*, timeout_seconds: int = 180) -> dict[str, object]:
    from PyQt5.QAxContainer import QAxWidget
    from PyQt5.QtWidgets import QApplication

    result: dict[str, object] = {"started_at": datetime.now().isoformat(), "environment": environment_diagnostic(), "processes": process_inventory(), "order_account_tr_requests": 0, "telegram_sends": 0, "cache_writes": 0}
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    result["qapplication_created"] = True
    result["active_x_probes"] = probe_active_x(QAxWidget)
    selected = result["active_x_probes"]["constructor_control"]
    if not selected["created"] or selected["is_null"] or not selected["dynamic_call_available"]:
        result["status"] = "ACTIVE_X_INVALID"
        return result
    adapter = KiwoomQAxAdapter()
    try:
        print("If the official Kiwoom login window opens, complete login there only.", flush=True)
        print("If automatic login is configured, leave the window open until completion.", flush=True)
        print("Do not close the login window or run KOA Studio/another OpenAPI program. Maximum wait: 180 seconds.", flush=True)
        result["login"] = diagnose_login_core(adapter, QtLoginWaiter(timeout_seconds * 1000))
        status = result["login"]["status"]
        if status == "TIMEOUT_NO_LOGIN_EVENT" and not result["environment"]["interactive_desktop"]:
            status = "GUI_SESSION_UNAVAILABLE"
        elif status == "TIMEOUT_NO_LOGIN_EVENT" and (result["processes"]["kiwoom"] or result["processes"]["koa_studio"] or result["processes"]["python"] > 1):
            status = "POSSIBLE_PROCESS_CONFLICT"
        result["status"] = status
        return result
    finally:
        adapter.close()


def print_login_diagnostic(result: dict[str, object]) -> bool:
    env = result.get("environment", {})
    print(f"EXECUTABLE={env.get('executable')}")
    print(f"PYTHON={env.get('python')}")
    print(f"BITS={env.get('bits')}")
    print(f"PID={env.get('pid')}")
    print(f"ADMINISTRATOR={env.get('administrator')}")
    print(f"GUI_SESSION={env.get('session_kind')}")
    print(f"PROCESSES={result.get('processes')}")
    print("If the official Kiwoom login window opens, complete login there only; do not close it before this diagnostic finishes.")
    print("Do not run KOA Studio or another Kiwoom OpenAPI program concurrently. Maximum wait: 180 seconds.")
    print(f"ACTIVE_X={result.get('active_x_probes')}")
    print(f"LOGIN={result.get('login', 'NOT_REACHED')}")
    print(f"KIWOOM LOGIN DIAGNOSTIC: {result.get('status')}")
    return result.get("status") == "LOGIN_SUCCESS"

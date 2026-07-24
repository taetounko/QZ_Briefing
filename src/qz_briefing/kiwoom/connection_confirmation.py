"""Safely dismiss only the Kiwoom OpenAPI connection-confirmation dialog."""

from __future__ import annotations

import ctypes
import logging
import threading
from datetime import datetime
from typing import Callable


LOGGER = logging.getLogger(__name__)
DIALOG_CLASS = "#32770"
BUTTON_CLASS = "Button"
ALLOWED_BUTTON_TEXT = {"확인"}
REQUIRED_TITLE_TOKENS = ("OPENAPI",)
REQUIRED_BODY_TOKENS = ("API", "접속")
FORBIDDEN_TOKENS = ("주문", "인증서", "계좌비밀번호", "비밀번호", "보안경고")
BM_CLICK = 0x00F5


class KiwoomConnectionConfirmationMonitor:
    """Poll in a daemon thread so dialog handling never blocks the Qt loop."""

    def __init__(self, *, poll_seconds: float = 0.25, max_seconds: float = 300.0, scanner: Callable[[], tuple[bool, bool, bool]] | None = None) -> None:
        self._poll_seconds = poll_seconds
        self._max_seconds = max_seconds
        self._scanner = scanner or _scan_and_click_once
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kiwoom-confirmation-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        deadline = self._max_seconds
        elapsed = 0.0
        while not self._stop.is_set() and elapsed < deadline:
            try:
                found, clicked, access_denied = self._scanner()
                if found:
                    LOGGER.info("Kiwoom API connection confirmation detected at %s; handled=%s", datetime.now().isoformat(), clicked)
                    if access_denied:
                        LOGGER.warning("Kiwoom API confirmation could not be handled because process privilege levels may differ")
                    if clicked:
                        return
            except Exception:
                LOGGER.warning("Kiwoom API confirmation monitor scan failed", exc_info=False)
            if self._stop.wait(self._poll_seconds):
                return
            elapsed += self._poll_seconds


def _window_text(user32: object, handle: int) -> str:
    length = int(user32.GetWindowTextLengthW(handle))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, len(buffer))
    return buffer.value.strip()


def _class_name(user32: object, handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(handle, buffer, len(buffer))
    return buffer.value.strip()


def is_safe_confirmation(*, title: str, window_class: str, button_text: str, body_texts: tuple[str, ...]) -> bool:
    """Require title, native dialog class, button text, and safe body semantics."""
    combined = " ".join((title, *body_texts)).upper()
    return (
        window_class == DIALOG_CLASS
        and button_text in ALLOWED_BUTTON_TEXT
        and all(token in title.upper() for token in REQUIRED_TITLE_TOKENS)
        and all(token.upper() in combined for token in REQUIRED_BODY_TOKENS)
        and not any(token.upper() in combined for token in FORBIDDEN_TOKENS)
    )


def _scan_and_click_once() -> tuple[bool, bool, bool]:
    """Return (safe dialog found, click succeeded, access denied)."""
    if not hasattr(ctypes, "windll"):
        return False, False, False
    user32 = ctypes.windll.user32
    found = False
    clicked = False
    access_denied = False
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def inspect_window(raw_handle: int, _parameter: int) -> bool:
        nonlocal found, clicked, access_denied
        handle = int(raw_handle)
        if not user32.IsWindowVisible(handle):
            return True
        title = _window_text(user32, handle)
        window_class = _class_name(user32, handle)
        children: list[tuple[int, str, str]] = []

        def inspect_child(raw_child: int, _child_parameter: int) -> bool:
            child = int(raw_child)
            children.append((child, _class_name(user32, child), _window_text(user32, child)))
            return True

        child_callback = WNDENUMPROC(inspect_child)
        user32.EnumChildWindows(handle, child_callback, 0)
        buttons = [(child, text) for child, klass, text in children if klass == BUTTON_CLASS and text in ALLOWED_BUTTON_TEXT]
        bodies = tuple(text for _child, klass, text in children if klass != BUTTON_CLASS and text)
        if len(buttons) != 1 or not is_safe_confirmation(title=title, window_class=window_class, button_text=buttons[0][1], body_texts=bodies):
            return True
        found = True
        ctypes.set_last_error(0)
        result = user32.SendMessageW(buttons[0][0], BM_CLICK, 0, 0)
        error = ctypes.get_last_error()
        clicked = error == 0 and result == 0
        access_denied = error == 5
        return not clicked

    callback = WNDENUMPROC(inspect_window)
    user32.EnumWindows(callback, 0)
    return found, clicked, access_denied

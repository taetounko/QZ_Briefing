# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import mimetypes
import urllib.parse
import urllib.request
from pathlib import Path

class TelegramError(RuntimeError): pass

class TelegramAdapter:
    """Small blocking adapter intended to be called only by the service worker."""
    def __init__(self, token: str, chat_id: str, *, opener=urllib.request.urlopen, timeout: float = 10):
        self._token, self._chat_id, self._opener, self._timeout = token, chat_id, opener, timeout
    def send_text(self, text: str, *, parse_mode: str | None = "MarkdownV2") -> None:
        payload = {"chat_id": self._chat_id, "text": text}
        if parse_mode: payload["parse_mode"] = parse_mode
        self._post("sendMessage", urllib.parse.urlencode(payload).encode())
    def send_document(self, path: Path, caption: str = "") -> None:
        boundary = "qzbriefingboundary"; data = Path(path).read_bytes(); name = Path(path).name
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{self._chat_id}\r\n".encode(), f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(), f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\nContent-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n".encode(), data, f"\r\n--{boundary}--\r\n".encode()]
        self._post("sendDocument", b"".join(parts), f"multipart/form-data; boundary={boundary}")
    def _post(self, method: str, data: bytes, content_type="application/x-www-form-urlencoded") -> None:
        # Never log this URL because it contains the secret path component.
        request = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/{method}", data=data, headers={"Content-Type": content_type})
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"): raise TelegramError("Telegram API rejected request")
        except TelegramError: raise
        except Exception as exc: raise TelegramError(f"Telegram transport failed: {type(exc).__name__}") from exc

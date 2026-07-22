# -*- coding: utf-8 -*-
from __future__ import annotations
import ctypes
import os
from pathlib import Path

class SecretStoreError(RuntimeError): pass

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

class DpapiSecretStore:
    """Current-user DPAPI store; plaintext is never written to disk."""
    def __init__(self, path: Path): self.path = Path(path)
    def save(self, secret: str) -> None:
        if os.name != "nt": raise SecretStoreError("Windows DPAPI is unavailable")
        raw = secret.encode("utf-8"); source_buffer = ctypes.create_string_buffer(raw)
        source = _Blob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte))); target = _Blob()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
            raise SecretStoreError("CryptProtectData failed")
        try:
            encrypted = ctypes.string_at(target.pbData, target.cbData)
            self.path.parent.mkdir(parents=True, exist_ok=True); self.path.write_bytes(encrypted)
        finally: ctypes.windll.kernel32.LocalFree(target.pbData)
    def load(self) -> str:
        if os.name != "nt": raise SecretStoreError("Windows DPAPI is unavailable")
        encrypted = self.path.read_bytes(); source_buffer = ctypes.create_string_buffer(encrypted)
        source = _Blob(len(encrypted), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte))); target = _Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
            raise SecretStoreError("CryptUnprotectData failed")
        try: return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
        finally: ctypes.windll.kernel32.LocalFree(target.pbData)
    def remove(self) -> None:
        try: self.path.unlink()
        except FileNotFoundError: pass

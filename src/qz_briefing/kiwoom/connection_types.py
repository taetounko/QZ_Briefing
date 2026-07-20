"""Types shared by the Kiwoom connection manager and its adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol, runtime_checkable


class ConnectionState(Enum):
    """Lifecycle states managed independently from the Kiwoom OCX."""

    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECT_WAIT = auto()
    FAILED = auto()
    STOPPED = auto()


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Timing and retry limits for connection monitoring."""

    check_interval_seconds: float = 30
    reconnect_delay_seconds: float = 60
    max_reconnect_attempts: int = 3
    login_timeout_seconds: float = 300

    def __post_init__(self) -> None:
        if self.check_interval_seconds < 0:
            raise ValueError("check_interval_seconds must be non-negative")
        if self.reconnect_delay_seconds < 0:
            raise ValueError("reconnect_delay_seconds must be non-negative")
        if self.max_reconnect_attempts < 1:
            raise ValueError("max_reconnect_attempts must be at least 1")
        if self.login_timeout_seconds <= 0:
            raise ValueError("login_timeout_seconds must be positive")


@runtime_checkable
class KiwoomConnection(Protocol):
    """Minimal connection-only boundary implemented later by an OCX adapter."""

    def get_connect_state(self) -> int:
        """Return the Kiwoom connection state, which must be 0 or 1."""

    def request_connect(self) -> int:
        """Return the immediate result of one login connection request."""


@dataclass(frozen=True, slots=True)
class ConnectionTransition:
    """A non-sensitive record of one connection state change."""

    previous_state: ConnectionState
    new_state: ConnectionState
    reason: str
    reconnect_attempts: int

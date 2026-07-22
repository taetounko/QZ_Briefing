from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class NotificationRequest:
    event_type: str
    trading_date: str
    text: str
    markdown_path: str | None = None
    json_path: str | None = None
    unique_nonce: str | None = None

@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    error: str | None = None

@dataclass
class NotificationStatus:
    configured: bool = False
    enabled: bool = False
    last_success_at: str | None = None
    last_event: str | None = None
    pending_count: int = 0
    last_error: str | None = None
    next_attempt_at: str | None = None

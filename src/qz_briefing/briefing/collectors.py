"""Collector contract and offline placeholder implementation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .models import BriefingContext


class BriefingCollector(Protocol):
    name: str

    def collect(self, context: BriefingContext) -> object: ...


class PlaceholderCollector:
    """Return a static marker until a real data source is connected."""

    name = "placeholder"

    def __init__(self, clock: Callable[[], datetime] = datetime.now) -> None:
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, str]:
        return {
            "collector": self.name,
            "status": "placeholder",
            "collected_at": self._clock().isoformat(),
            "message": (
                f"No market-data collector is connected for "
                f"{context.briefing_type.value}."
            ),
        }

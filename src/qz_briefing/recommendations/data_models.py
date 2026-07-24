"""Source-aware, offline recommendation data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class DataMetadata:
    code: str
    name: str
    market: str
    as_of: datetime
    source: str
    updated_at: datetime
    complete: bool = True
    missing: bool = False
    confidence: float = 1.0
    collection_error: str | None = None
    used_previous_trading_day: bool = False


@dataclass(frozen=True)
class StockMasterRecord:
    metadata: DataMetadata
    security_type: str
    tradable: bool = True
    trading_status: str = "normal"
    risk_labels: tuple[str, ...] = ()
    listed_date: date | None = None
    reference_price: float | None = None
    raw_state: str = ""


@dataclass(frozen=True)
class DailyBar:
    metadata: DataMetadata
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_value: float | None
    adjusted: bool


@dataclass(frozen=True)
class AggregatedWeeklyBar:
    metadata: DataMetadata
    week_start: date
    week_end: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_value: float | None


@dataclass(frozen=True)
class InvestorFlowSnapshot:
    metadata: DataMetadata
    foreign_daily: tuple[float, ...] = ()
    institution_daily: tuple[float, ...] = ()


@dataclass(frozen=True)
class ProgramFlowSnapshot:
    metadata: DataMetadata
    daily_net_buy: tuple[float, ...] = ()


@dataclass(frozen=True)
class FundamentalSnapshot:
    metadata: DataMetadata
    quarter: str
    revenue_growth: float | None = None
    operating_profit_growth: float | None = None
    net_income_growth: float | None = None
    turned_profitable: bool | None = None
    earnings_date: date | None = None
    versus_consensus: float | None = None


@dataclass(frozen=True)
class CatalystRecord:
    metadata: DataMetadata
    category: str
    summary: str
    announced_at: datetime | None
    sentiment: str = "neutral"
    verified: bool = False
    valid_until: datetime | None = None
    priced_in_likelihood: float | None = None


@dataclass(frozen=True)
class RiskEvent:
    metadata: DataMetadata
    risk_type: str
    severity: float
    occurred_at: datetime
    valid_until: datetime | None
    hard_exclusion: bool
    deduction: float
    display: str


@dataclass(frozen=True)
class PriceFeatures:
    values: dict[str, float | bool]
    missing: tuple[str, ...] = ()
    confidence: float = 1.0


@dataclass(frozen=True)
class RecommendationDataBundle:
    master: StockMasterRecord
    daily_bars: tuple[DailyBar, ...]
    weekly_bars: tuple[AggregatedWeeklyBar, ...]
    price_features: PriceFeatures
    investor_flow: InvestorFlowSnapshot | None = None
    program_flow: ProgramFlowSnapshot | None = None
    fundamentals: tuple[FundamentalSnapshot, ...] = ()
    catalysts: tuple[CatalystRecord, ...] = ()
    risks: tuple[RiskEvent, ...] = ()


@dataclass
class DataQualityReport:
    total_count: int = 0
    evaluable_count: int = 0
    partial_count: int = 0
    failures: list["CollectionFailure"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.evaluable_count / self.total_count if self.total_count else 0.0


@dataclass(frozen=True)
class CollectionFailure:
    code: str
    data_kind: str
    reason: str
    occurred_at: datetime
    retry_count: int = 0

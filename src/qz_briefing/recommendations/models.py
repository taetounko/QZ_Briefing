from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class WeeklyBar:
    ended_at: datetime
    open: float
    high: float
    low: float
    close: float
    completed: bool = True


@dataclass(frozen=True)
class StockUniverseItem:
    market: str
    code: str
    name: str
    security_type: str = "common_stock"
    tradable: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class CatalystEvidence:
    summary: str
    source: str
    observed_at: datetime


@dataclass(frozen=True)
class RiskFlag:
    code: str
    reason: str
    deduction: float


@dataclass(frozen=True)
class RecommendationFeatures:
    item: StockUniverseItem
    as_of: datetime
    weekly_bars: tuple[WeeklyBar, ...]
    bottom_rebound: float | None = None
    fund_inflow: float | None = None
    daily_trend: float | None = None
    catalyst_strength: float | None = None
    liquidity: float | None = None
    confidence: float = 0.5
    trading_value: float | None = None
    foreign_flow: float | None = None
    institution_flow: float | None = None
    program_flow: float | None = None
    catalyst_evidence: tuple[CatalystEvidence, ...] = ()
    risks: tuple[RiskFlag, ...] = ()
    missing: tuple[str, ...] = ()
    preferred_entry: str = "관찰 우선"
    invalidation_conditions: tuple[str, ...] = ()
    foreign_net_5d: float | None = None
    foreign_net_20d: float | None = None
    institution_net_5d: float | None = None
    institution_net_20d: float | None = None
    foreign_buy_days_5d: int = 0
    institution_buy_days_5d: int = 0
    foreign_normalized_5d: float | None = None
    foreign_normalized_20d: float | None = None
    institution_normalized_5d: float | None = None
    institution_normalized_20d: float | None = None
    joint_buy_5d: bool = False
    flow_acceleration: float | None = None
    fund_flow_score: float | None = None
    fund_flow_reasons: tuple[str, ...] = ()
    fund_flow_status: str = "data_unavailable"
    horizon: str = "단기·스윙"


@dataclass(frozen=True)
class RecommendationPolicy:
    weekly_weight: float = 20
    bottom_weight: float = 20
    fund_weight: float = 25
    daily_weight: float = 15
    catalyst_weight: float = 15
    liquidity_weight: float = 5
    strong_threshold: float = 72
    review_threshold: float = 52
    strong_limit: int = 3
    review_limit: int = 3
    total_limit: int = 6
    allowed_markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    allowed_security_types: tuple[str, ...] = ("common_stock",)


@dataclass(frozen=True)
class WeeklySignal:
    weekly_close: float
    weekly_ma5: float
    weekly_close_above_ma5: bool
    distance_rate: float
    consecutive_weeks: int
    ma5_slope_rate: float | None
    upper_wick_rate: float
    completed_at: datetime


@dataclass
class RecommendationScore:
    item: StockUniverseItem
    eligible: bool
    exclusion_reasons: list[str]
    weekly: WeeklySignal | None
    components: dict[str, float]
    gross_score: float
    risk_deduction: float
    total_score: float
    confidence: float
    reasons: list[str]
    missing: list[str]
    risks: list[str]
    features: RecommendationFeatures


@dataclass
class StockRecommendation:
    rank: int
    grade: str
    score: RecommendationScore


@dataclass
class DailyRecommendationReport:
    as_of: datetime
    input_count: int
    hard_filter_pass_count: int
    excluded: list[dict[str, str]] = field(default_factory=list)
    strong: list[StockRecommendation] = field(default_factory=list)
    review: list[StockRecommendation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

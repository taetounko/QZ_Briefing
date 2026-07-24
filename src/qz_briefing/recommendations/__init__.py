"""Explainable, offline stock recommendation domain."""

from .models import (
    CatalystEvidence, RecommendationFeatures, RecommendationPolicy, RiskFlag,
    StockUniverseItem, WeeklyBar,
)
from .scoring import evaluate_candidate
from .selector import select_recommendations
from .renderer import render_recommendations

__all__ = [
    "CatalystEvidence", "RecommendationFeatures", "RecommendationPolicy",
    "RiskFlag", "StockUniverseItem", "WeeklyBar", "evaluate_candidate",
    "select_recommendations", "render_recommendations",
]

"""
Flow-mode filters and scoring for high-liquidity, high-volume trades.

The FlowMode class encapsulates guardrails inspired by cup-and-handle style
momentum ("Cupsey-style") setups. It focuses on:
- substantial liquidity
- sustained 24h volume and transaction history
- healthy, but not extreme, short-term buy pressure
- safeguards against fake spikes and illiquid traps
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


def safe_get(obj: Dict[str, Any], path: str, default: Any = None) -> Any:
    """Safely traverse nested dicts using dotted paths like "a.b.c"."""
    cur: Any = obj
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


@dataclass(frozen=True)
class FlowThresholds:
    min_liquidity_usd: float = 20_000
    min_volume_24h: float = 100_000
    min_txns_24h: int = 200
    min_avg_trade_usd: float = 300
    min_age_sec: int = 600
    max_age_sec: int = 21_600
    min_buys_5m: int = 15
    min_sells_5m: int = 5
    min_buy_sell_ratio: float = 1.2
    max_buy_sell_ratio: float = 12
    max_vol_spike_share: float = 0.25  # 5m volume cannot exceed 25% of 24h volume


class FlowMode:
    """Evaluate and score pairs for high-liquidity, high-flow setups."""

    def __init__(self, thresholds: FlowThresholds = FlowThresholds()):
        self.thresholds = thresholds

    def is_valid_flow(self, pair: Dict[str, Any]) -> bool:
        """
        Apply Cupsey-style flow filters to a Dexscreener pair payload.

        Returns True when the pair passes all guardrails.
        """
        liq_usd = safe_get(pair, "liquidity.usd", 0) or 0
        if liq_usd < self.thresholds.min_liquidity_usd:
            return False

        vol_24h = safe_get(pair, "volume.h24", 0) or 0
        if vol_24h < self.thresholds.min_volume_24h:
            return False

        buys_24h = safe_get(pair, "txns.h24.buys", 0) or 0
        sells_24h = safe_get(pair, "txns.h24.sells", 0) or 0
        txns_24h = buys_24h + sells_24h
        if txns_24h < self.thresholds.min_txns_24h:
            return False

        avg_trade = vol_24h / txns_24h if txns_24h > 0 else 0
        if avg_trade < self.thresholds.min_avg_trade_usd:
            return False

        age_sec = self._compute_age_sec(pair)
        if age_sec is None:
            return False
        if not (self.thresholds.min_age_sec <= age_sec <= self.thresholds.max_age_sec):
            return False

        buys_5m = safe_get(pair, "txns.m5.buys", 0) or 0
        sells_5m = safe_get(pair, "txns.m5.sells", 0) or 0

        if sells_5m == 0:
            return False
        if buys_5m < self.thresholds.min_buys_5m or sells_5m < self.thresholds.min_sells_5m:
            return False

        ratio = buys_5m / sells_5m if sells_5m > 0 else 0
        if ratio < self.thresholds.min_buy_sell_ratio:
            return False
        if ratio > self.thresholds.max_buy_sell_ratio:
            return False

        vol_5m = safe_get(pair, "volume.m5", 0) or 0
        if vol_5m > self.thresholds.max_vol_spike_share * vol_24h:
            return False

        return True

    def compute_flow_score(self, pair: Dict[str, Any]) -> int:
        """
        Compute a 0–100 score incorporating liquidity depth, 24h volume,
        average trade size, and short-term buy pressure.
        """
        liq_usd = safe_get(pair, "liquidity.usd", 0) or 0
        vol_24h = safe_get(pair, "volume.h24", 0) or 0

        buys_24h = safe_get(pair, "txns.h24.buys", 0) or 0
        sells_24h = safe_get(pair, "txns.h24.sells", 0) or 0
        txns_24h = buys_24h + sells_24h
        avg_trade = vol_24h / txns_24h if txns_24h > 0 else 0

        buys_5m = safe_get(pair, "txns.m5.buys", 0) or 0
        sells_5m = safe_get(pair, "txns.m5.sells", 0) or 0

        score = 0
        score += self._liquidity_score(liq_usd)
        score += self._volume_score(vol_24h)
        score += self._avg_trade_score(avg_trade)
        score += self._buy_pressure_score(buys_5m, sells_5m)

        return max(0, min(100, int(score)))

    def _compute_age_sec(self, pair: Dict[str, Any]) -> Optional[int]:
        created_ms = safe_get(pair, "pairCreatedAt", None)
        if created_ms is None:
            return None
        try:
            now_ms = int(time.time() * 1000)
            return max(0, int((now_ms - int(created_ms)) / 1000))
        except (TypeError, ValueError):
            return None

    def _liquidity_score(self, liq_usd: float) -> float:
        if liq_usd >= 80_000:
            return 25
        if liq_usd >= 60_000:
            return 22
        if liq_usd >= 45_000:
            return 20
        if liq_usd >= 30_000:
            return 17
        if liq_usd >= self.thresholds.min_liquidity_usd:
            return 12
        return 0

    def _volume_score(self, vol_24h: float) -> float:
        if vol_24h >= 500_000:
            return 25
        if vol_24h >= 350_000:
            return 21
        if vol_24h >= 200_000:
            return 17
        if vol_24h >= self.thresholds.min_volume_24h:
            return 12
        return 0

    def _avg_trade_score(self, avg_trade: float) -> float:
        if avg_trade >= 2_000:
            return 20
        if avg_trade >= 1_200:
            return 16
        if avg_trade >= 700:
            return 12
        if avg_trade >= self.thresholds.min_avg_trade_usd:
            return 8
        return 0

    def _buy_pressure_score(self, buys_5m: float, sells_5m: float) -> float:
        if buys_5m <= 0 or sells_5m <= 0:
            return 0

        ratio = buys_5m / sells_5m if sells_5m else 0

        if buys_5m >= 50 and ratio >= 4:
            return 30
        if buys_5m >= 35 and ratio >= 2.5:
            return 25
        if buys_5m >= 20 and ratio >= 1.8:
            return 20
        if buys_5m >= self.thresholds.min_buys_5m and ratio >= self.thresholds.min_buy_sell_ratio:
            return 15
        return 8


__all__ = ["FlowMode", "FlowThresholds", "safe_get"]

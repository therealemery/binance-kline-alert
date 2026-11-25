from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class PatternType(str, Enum):
    BULLISH_ENGULFING = "bullish_engulfing"  # 阳包阴
    BEARISH_ENGULFING = "bearish_engulfing"  # 阴包阳

    @property
    def cn_name(self) -> str:
        if self is PatternType.BULLISH_ENGULFING:
            return "阳包阴"
        return "阴包阳"

    @property
    def emoji(self) -> str:
        if self is PatternType.BULLISH_ENGULFING:
            return "📈"
        return "📉"


@dataclass(frozen=True)
class Kline:
    symbol: str
    interval: str
    open_time: int   # ms
    close_time: int  # ms
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def body_pct(self) -> Decimal:
        if self.open == 0:
            return Decimal("0")
        return self.body / self.open

    @property
    def change_pct(self) -> Decimal:
        if self.open == 0:
            return Decimal("0")
        return (self.close - self.open) / self.open * Decimal("100")


@dataclass(frozen=True)
class PatternSignal:
    symbol: str
    display_name: str
    interval: str
    pattern: PatternType
    prev_kline: Kline
    curr_kline: Kline
    body_ratio: Decimal
    volume_ratio: Decimal | None

    @property
    def open_time(self) -> int:
        return self.curr_kline.open_time


@dataclass(frozen=True)
class WatchedSymbol:
    symbol: str
    display_name: str
    enabled: bool = True
    added_by: str = "config"
    created_at: str = ""

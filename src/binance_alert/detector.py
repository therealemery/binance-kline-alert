from __future__ import annotations

from decimal import Decimal

from .models import Kline, PatternSignal, PatternType


class EngulfingDetector:
    """Detects bullish (阳包阴) and bearish (阴包阳) engulfing patterns on closed candles."""

    def __init__(
        self,
        *,
        enable_volume_filter: bool = True,
        min_body_ratio: Decimal = Decimal("1.0"),
        min_body_pct: Decimal = Decimal("0.001"),
    ) -> None:
        self.enable_volume_filter = enable_volume_filter
        self.min_body_ratio = min_body_ratio
        self.min_body_pct = min_body_pct

    def detect(
        self,
        prev_kline: Kline,
        curr_kline: Kline,
        *,
        display_name: str | None = None,
    ) -> PatternSignal | None:
        if prev_kline.symbol != curr_kline.symbol or prev_kline.interval != curr_kline.interval:
            raise ValueError("Klines must share the same symbol and interval")
        if curr_kline.open_time <= prev_kline.open_time:
            return None

        prev_body = prev_kline.body
        curr_body = curr_kline.body
        if prev_body <= 0 or curr_body <= 0:
            return None

        if self.min_body_pct > 0:
            if prev_kline.body_pct < self.min_body_pct or curr_kline.body_pct < self.min_body_pct:
                return None

        body_ratio = curr_body / prev_body
        if body_ratio < self.min_body_ratio:
            return None

        volume_ratio: Decimal | None = None
        if prev_kline.volume > 0:
            volume_ratio = curr_kline.volume / prev_kline.volume

        if self.enable_volume_filter:
            if curr_kline.volume <= prev_kline.volume:
                return None

        pattern: PatternType | None = None

        # 阳包阴 (Bullish Engulfing): prev bearish, curr bullish, curr body wraps prev body
        if (
            prev_kline.is_bearish
            and curr_kline.is_bullish
            and curr_kline.open <= prev_kline.close
            and curr_kline.close >= prev_kline.open
            and (curr_kline.open < prev_kline.close or curr_kline.close > prev_kline.open)
        ):
            pattern = PatternType.BULLISH_ENGULFING

        # 阴包阳 (Bearish Engulfing): prev bullish, curr bearish, curr body wraps prev body
        elif (
            prev_kline.is_bullish
            and curr_kline.is_bearish
            and curr_kline.open >= prev_kline.close
            and curr_kline.close <= prev_kline.open
            and (curr_kline.open > prev_kline.close or curr_kline.close < prev_kline.open)
        ):
            pattern = PatternType.BEARISH_ENGULFING

        if pattern is None:
            return None

        return PatternSignal(
            symbol=curr_kline.symbol,
            display_name=display_name or curr_kline.symbol,
            interval=curr_kline.interval,
            pattern=pattern,
            prev_kline=prev_kline,
            curr_kline=curr_kline,
            body_ratio=body_ratio,
            volume_ratio=volume_ratio,
        )

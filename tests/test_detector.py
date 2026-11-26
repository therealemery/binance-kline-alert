from decimal import Decimal

from binance_alert.detector import EngulfingDetector
from binance_alert.models import Kline, PatternType


def _kline(
    open_time: int,
    open_: str,
    close: str,
    volume: str = "100",
    *,
    symbol: str = "NVDABUSDT",
    interval: str = "1h",
) -> Kline:
    o = Decimal(open_)
    c = Decimal(close)
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + 3_599_999,
        open=o,
        high=max(o, c) + Decimal("0.5"),
        low=min(o, c) - Decimal("0.5"),
        close=c,
        volume=Decimal(volume),
    )


def test_bullish_engulfing_detected():
    detector = EngulfingDetector(enable_volume_filter=True, min_body_ratio=Decimal("1.0"))
    prev = _kline(1_000_000, "162.50", "161.20", "1000")
    curr = _kline(4_600_000, "161.10", "163.80", "1500")

    sig = detector.detect(prev, curr, display_name="NVDAB (NVIDIA)")
    assert sig is not None
    assert sig.pattern is PatternType.BULLISH_ENGULFING
    assert sig.volume_ratio == Decimal("1.5")
    assert sig.body_ratio > Decimal("2.0")


def test_bearish_engulfing_detected():
    detector = EngulfingDetector(enable_volume_filter=True)
    prev = _kline(1_000_000, "100.0", "102.0", "500")
    curr = _kline(4_600_000, "102.2", "99.5", "800")

    sig = detector.detect(prev, curr)
    assert sig is not None
    assert sig.pattern is PatternType.BEARISH_ENGULFING


def test_volume_filter_blocks_low_volume():
    detector = EngulfingDetector(enable_volume_filter=True)
    prev = _kline(1_000_000, "162.50", "161.20", "2000")
    curr = _kline(4_600_000, "161.10", "163.80", "1500")

    assert detector.detect(prev, curr) is None

    relaxed = EngulfingDetector(enable_volume_filter=False)
    assert relaxed.detect(prev, curr) is not None


def test_min_body_ratio_enforced():
    detector = EngulfingDetector(enable_volume_filter=False, min_body_ratio=Decimal("1.5"))
    prev = _kline(1_000_000, "102.0", "100.0", "100")  # body 2.0
    curr = _kline(4_600_000, "99.8", "102.2", "200")   # body 2.4 -> ratio 1.2 < 1.5
    assert detector.detect(prev, curr) is None


def test_doji_filtered_by_min_body_pct():
    detector = EngulfingDetector(enable_volume_filter=False, min_body_pct=Decimal("0.002"))
    prev = _kline(1_000_000, "100.05", "100.00", "100")  # 0.05% < 0.2%
    curr = _kline(4_600_000, "99.90", "101.00", "200")
    assert detector.detect(prev, curr) is None


def test_partial_overlap_not_engulfing():
    detector = EngulfingDetector(enable_volume_filter=False)
    prev = _kline(1_000_000, "105.0", "100.0", "100")
    curr = _kline(4_600_000, "100.5", "106.0", "200")  # open > prev.close
    assert detector.detect(prev, curr) is None


def test_min_change_pct_filters_small_moves():
    detector = EngulfingDetector(enable_volume_filter=True, min_change_pct=Decimal("3.0"))

    # Bullish engulfing with +2.5% move -> blocked
    prev_bull = _kline(1_000_000, "101.0", "100.0", "100")
    curr_bull_small = _kline(4_600_000, "100.0", "102.5", "150")
    assert detector.detect(prev_bull, curr_bull_small) is None

    # Bullish engulfing with exact +3.0% move -> passes
    curr_bull_ok = _kline(4_600_000, "100.0", "103.0", "150")
    sig_bull = detector.detect(prev_bull, curr_bull_ok)
    assert sig_bull is not None
    assert sig_bull.pattern is PatternType.BULLISH_ENGULFING

    # Bearish engulfing with -2.9% move -> blocked
    prev_bear = _kline(1_000_000, "99.0", "100.0", "100")
    curr_bear_small = _kline(4_600_000, "100.0", "97.1", "150")
    assert detector.detect(prev_bear, curr_bear_small) is None

    # Bearish engulfing with exact -3.0% move -> passes
    curr_bear_ok = _kline(4_600_000, "100.0", "97.0", "150")
    sig_bear = detector.detect(prev_bear, curr_bear_ok)
    assert sig_bear is not None
    assert sig_bear.pattern is PatternType.BEARISH_ENGULFING

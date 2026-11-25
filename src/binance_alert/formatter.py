from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import Kline, PatternSignal, PatternType

KNOWN_STOCK_NAMES: dict[str, str] = {
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "GOOG": "Alphabet",
    "META": "Meta",
    "COIN": "Coinbase",
    "MSTR": "Strategy",
    "AMD": "AMD",
    "NFLX": "Netflix",
    "PLTR": "Palantir",
    "SPY": "SPDR S&P 500",
    "QQQ": "Invesco QQQ",
}

INTERVAL_LABELS: dict[str, str] = {
    "1m": "1 分钟",
    "3m": "3 分钟",
    "5m": "5 分钟",
    "15m": "15 分钟",
    "30m": "30 分钟",
    "1h": "60 分钟",
    "2h": "2 小时",
    "4h": "4 小时",
    "6h": "6 小时",
    "8h": "8 小时",
    "12h": "12 小时",
    "1d": "日线",
    "1w": "周线",
}


def infer_display_name(symbol: str) -> str:
    sym = symbol.upper()
    if sym.endswith("BUSDT") and len(sym) > 5:
        ticker = sym[:-5]
        if ticker in KNOWN_STOCK_NAMES:
            return f"{sym[:-4]} ({KNOWN_STOCK_NAMES[ticker]})"
        return sym[:-4]
    if sym.endswith("USDT") and len(sym) > 4:
        return f"{sym[:-4]}/USDT"
    return sym


def build_trade_url(symbol: str) -> str:
    sym = symbol.upper()
    for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB"):
        if sym.endswith(quote) and len(sym) > len(quote):
            base = sym[: -len(quote)]
            return f"https://www.binance.com/zh-CN/trade/{base}_{quote}"
    return f"https://www.binance.com/zh-CN/trade/{sym}"


def format_price(value: Decimal) -> str:
    abs_val = abs(value)
    if abs_val >= Decimal("1000"):
        return f"{value:,.2f}"
    if abs_val >= Decimal("1"):
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    if abs_val >= Decimal("0.01"):
        return f"{value:.5f}".rstrip("0").rstrip(".") or "0"
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def format_timestamp(ms: int, tz_name: str = "Asia/Shanghai") -> str:
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
    offset = dt.strftime("%z")
    tz_short = f"UTC{offset[:3]}" if offset else tz_name
    return f"{dt.strftime('%Y-%m-%d %H:%M')} ({tz_short})"


def format_signal_markdown(signal: PatternSignal, tz_name: str = "Asia/Shanghai") -> str:
    prev = signal.prev_kline
    curr = signal.curr_kline
    prev_dir = "阴线" if prev.is_bearish else "阳线"
    curr_dir = "阳线" if curr.is_bullish else "阴线"
    change_str = f"{curr.change_pct:+.2f}%"
    change_label = "涨幅" if signal.pattern is PatternType.BULLISH_ENGULFING else "跌幅"
    interval_label = INTERVAL_LABELS.get(signal.interval, signal.interval)
    time_str = format_timestamp(curr.open_time, tz_name)
    trade_url = build_trade_url(signal.symbol)

    color = "info" if signal.pattern is PatternType.BULLISH_ENGULFING else "warning"
    vol_line = ""
    if signal.volume_ratio is not None:
        vol_line = f"\n> 成交量比：{signal.volume_ratio:.2f}x（实体比 {signal.body_ratio:.2f}x）"

    return (
        f"### {signal.pattern.emoji} <font color=\"{color}\">{signal.pattern.cn_name}信号</font>\n\n"
        f"> **标的**：{signal.display_name}（`{signal.symbol}`）\n"
        f"> **周期**：{interval_label}\n"
        f"> **K线时间**：{time_str}\n"
        f"> **前一根**：{prev_dir} `{format_price(prev.open)}` → `{format_price(prev.close)}`\n"
        f"> **当前根**：{curr_dir} `{format_price(curr.open)}` → `{format_price(curr.close)}`\n"
        f"> **{change_label}**：`{change_str}`{vol_line}\n\n"
        f"[查看币安图表]({trade_url})"
    )


def format_kline_summary(
    symbol: str,
    display_name: str,
    prev: Kline,
    curr: Kline,
    signal: PatternSignal | None,
    tz_name: str = "Asia/Shanghai",
) -> str:
    prev_dir = "阳" if prev.is_bullish else ("阴" if prev.is_bearish else "平")
    curr_dir = "阳" if curr.is_bullish else ("阴" if curr.is_bearish else "平")
    status = f"{signal.pattern.emoji} 触发 **{signal.pattern.cn_name}**" if signal else "未触发吞没形态"
    return (
        f"**{display_name}** (`{symbol}`)\n"
        f"- 最新已收盘：{format_timestamp(curr.open_time, tz_name)}\n"
        f"- 前一根({prev_dir})：`{format_price(prev.open)}` → `{format_price(prev.close)}` (量 {prev.volume:.2f})\n"
        f"- 当前根({curr_dir})：`{format_price(curr.open)}` → `{format_price(curr.close)}` (`{curr.change_pct:+.2f}%`, 量 {curr.volume:.2f})\n"
        f"- 判定：{status}"
    )


def format_custom_rule_alert_markdown(
    *,
    rule_id: int | None,
    rule_name: str,
    rule_description: str,
    symbol: str,
    display_name: str,
    interval: str,
    curr: Kline,
    prev: Kline | None = None,
    tz_name: str = "Asia/Shanghai",
) -> str:
    interval_label = INTERVAL_LABELS.get(interval, interval)
    time_str = format_timestamp(curr.open_time, tz_name)
    trade_url = build_trade_url(symbol)
    curr_dir = "阳线" if curr.is_bullish else ("阴线" if curr.is_bearish else "平盘")
    color = "info" if curr.is_bullish else "warning"
    emoji = "🎯"
    id_tag = f" #{rule_id}" if rule_id is not None else ""

    prev_line = ""
    if prev is not None:
        prev_dir = "阳线" if prev.is_bullish else ("阴线" if prev.is_bearish else "平盘")
        prev_line = f"\n> **前一根**：{prev_dir} `{format_price(prev.open)}` → `{format_price(prev.close)}`"

    return (
        f"### {emoji} <font color=\"{color}\">自定义形态触发：{rule_name}{id_tag}</font>\n\n"
        f"> **标的**：{display_name}（`{symbol}`）\n"
        f"> **周期**：{interval_label}\n"
        f"> **K线时间**：{time_str}"
        f"{prev_line}\n"
        f"> **当前根**：{curr_dir} `{format_price(curr.open)}` → `{format_price(curr.close)}`（`{curr.change_pct:+.2f}%`）\n"
        f"> **规则说明**：{rule_description}\n\n"
        f"[查看币安图表]({trade_url})"
    )

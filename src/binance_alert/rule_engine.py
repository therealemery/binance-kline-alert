from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Callable

from .models import Kline

ALLOWED_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w",
}

ALLOWED_CANDLE_ATTRS = frozenset({
    "open",
    "high",
    "low",
    "close",
    "volume",
    "body",
    "body_pct",
    "change_pct",
    "range",
    "upper_shadow",
    "lower_shadow",
    "is_bull",
    "is_bear",
})

ALLOWED_FUNCTIONS = frozenset({
    "k",
    "ma",
    "sma",
    "ema",
    "vol_ma",
    "rsi",
    "highest_high",
    "lowest_low",
    "highest_close",
    "lowest_close",
    "bullish_engulfing",
    "bearish_engulfing",
    "hammer",
    "shooting_star",
    "inside_bar",
    "three_white_soldiers",
    "three_black_crows",
    "abs",
    "min",
    "max",
})


class UnsafeExpressionError(ValueError):
    """Raised when a rule expression violates the safe DSL AST whitelist."""


class InsufficientDataError(ValueError):
    """Raised when not enough closed candles are available for an expression."""


@dataclass(frozen=True)
class CustomRule:
    id: int | None
    name: str
    description: str
    symbols: list[str]  # ["*"] means all watched symbols
    interval: str
    expression: str
    lookback: int = 20
    enabled: bool = True
    source_prompt: str = ""
    created_by: str = "user"
    created_at: str = ""


class CandleView:
    """Read-only numeric view of a closed Kline exposed to the rule DSL."""

    __slots__ = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "body",
        "body_pct",
        "change_pct",
        "range",
        "upper_shadow",
        "lower_shadow",
        "is_bull",
        "is_bear",
    )

    def __init__(self, kline: Kline) -> None:
        self.open = float(kline.open)
        self.high = float(kline.high)
        self.low = float(kline.low)
        self.close = float(kline.close)
        self.volume = float(kline.volume)
        self.body = abs(self.close - self.open)
        self.body_pct = (self.body / self.open) if self.open > 0 else 0.0
        self.change_pct = ((self.close - self.open) / self.open * 100.0) if self.open > 0 else 0.0
        self.range = self.high - self.low
        self.upper_shadow = max(0.0, self.high - max(self.open, self.close))
        self.lower_shadow = max(0.0, min(self.open, self.close) - self.low)
        self.is_bull = self.close > self.open
        self.is_bear = self.close < self.open


def validate_expression(expression: str) -> ast.Expression:
    """Parse and strictly validate a DSL expression against the safe AST whitelist."""
    expr_str = expression.strip()
    if not expr_str or len(expr_str) > 800:
        raise UnsafeExpressionError("规则表达式为空或超出长度限制（800 字符）")

    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"表达式语法错误：{exc.msg}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load)):
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            continue
        if isinstance(node, (ast.And, ast.Or)):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
            continue
        if isinstance(node, (ast.Not, ast.UAdd, ast.USub)):
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            continue
        if isinstance(node, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            continue
        if isinstance(node, ast.Compare):
            continue
        if isinstance(node, (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
            continue
        if isinstance(node, ast.Name):
            if node.id in {"curr", "prev", "True", "False"} or node.id in ALLOWED_FUNCTIONS:
                continue
            raise UnsafeExpressionError(f"不允许的标识符：{node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr not in ALLOWED_CANDLE_ATTRS:
                raise UnsafeExpressionError(f"不允许的 K 线属性：{node.attr}")
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
                func_name = ast.unparse(node.func)
                raise UnsafeExpressionError(f"不允许的函数调用：{func_name}")
            if len(node.args) + len(node.keywords) > 6:
                raise UnsafeExpressionError(f"函数参数过多：{node.func.id}")
            continue
        if isinstance(node, ast.keyword):
            continue
        raise UnsafeExpressionError(f"不允许的语法节点：{type(node).__name__}")

    return tree


class _RuleEvalContext:
    def __init__(self, candles: list[CandleView]) -> None:
        # candles ordered oldest -> newest; candles[-1] is offset 0 (curr)
        self.candles = candles

    def k(self, offset: int = 0) -> CandleView:
        idx = int(offset)
        if idx < 0:
            raise UnsafeExpressionError("k(offset) 的 offset 不能为负数")
        pos = len(self.candles) - 1 - idx
        if pos < 0:
            raise InsufficientDataError(f"需要至少 {idx + 1} 根已收盘 K 线，当前仅有 {len(self.candles)} 根")
        return self.candles[pos]

    def _slice_ending(self, period: int, offset: int = 0) -> list[CandleView]:
        p = int(period)
        o = int(offset)
        if p < 1 or p > 200 or o < 0:
            raise UnsafeExpressionError(f"无效周期参数：period={period}, offset={offset}")
        end = len(self.candles) - o
        start = end - p
        if start < 0 or end <= 0:
            raise InsufficientDataError(f"需要至少 {p + o} 根已收盘 K 线")
        return self.candles[start:end]

    def ma(self, period: int, offset: int = 0) -> float:
        window = self._slice_ending(period, offset)
        return sum(c.close for c in window) / len(window)

    def sma(self, period: int, offset: int = 0) -> float:
        return self.ma(period, offset)

    def vol_ma(self, period: int, offset: int = 0) -> float:
        window = self._slice_ending(period, offset)
        return sum(c.volume for c in window) / len(window)

    def ema(self, period: int, offset: int = 0) -> float:
        p = int(period)
        o = int(offset)
        if p < 1 or p > 200 or o < 0:
            raise UnsafeExpressionError(f"无效 EMA 参数：period={period}, offset={offset}")
        end = len(self.candles) - o
        if end < p:
            raise InsufficientDataError(f"EMA({p}) 需要至少 {p + o} 根已收盘 K 线")
        series = self.candles[:end]
        alpha = 2.0 / (p + 1.0)
        val = sum(c.close for c in series[:p]) / p
        for c in series[p:]:
            val = c.close * alpha + val * (1.0 - alpha)
        return val

    def rsi(self, period: int = 14, offset: int = 0) -> float:
        p = int(period)
        o = int(offset)
        if p < 2 or p > 100 or o < 0:
            raise UnsafeExpressionError(f"无效 RSI 参数：period={period}, offset={offset}")
        end = len(self.candles) - o
        if end < p + 1:
            raise InsufficientDataError(f"RSI({p}) 需要至少 {p + o + 1} 根已收盘 K 线")
        closes = [c.close for c in self.candles[:end]]
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        avg_gain = sum(max(d, 0.0) for d in deltas[:p]) / p
        avg_loss = sum(max(-d, 0.0) for d in deltas[:p]) / p
        for d in deltas[p:]:
            gain = max(d, 0.0)
            loss = max(-d, 0.0)
            avg_gain = (avg_gain * (p - 1) + gain) / p
            avg_loss = (avg_loss * (p - 1) + loss) / p
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def highest_high(self, period: int, offset: int = 0) -> float:
        return max(c.high for c in self._slice_ending(period, offset))

    def lowest_low(self, period: int, offset: int = 0) -> float:
        return min(c.low for c in self._slice_ending(period, offset))

    def highest_close(self, period: int, offset: int = 0) -> float:
        return max(c.close for c in self._slice_ending(period, offset))

    def lowest_close(self, period: int, offset: int = 0) -> float:
        return min(c.close for c in self._slice_ending(period, offset))

    def bullish_engulfing(
        self,
        min_body_ratio: float = 1.0,
        vol_mult: float = 0.0,
        min_body_pct: float = 0.001,
    ) -> bool:
        prev = self.k(1)
        curr = self.k(0)
        if prev.body <= 0 or curr.body <= 0:
            return False
        if prev.body_pct < min_body_pct or curr.body_pct < min_body_pct:
            return False
        if curr.body / prev.body < min_body_ratio:
            return False
        if vol_mult > 0 and curr.volume < prev.volume * vol_mult:
            return False
        return (
            prev.is_bear
            and curr.is_bull
            and curr.open <= prev.close
            and curr.close >= prev.open
            and (curr.open < prev.close or curr.close > prev.open)
        )

    def bearish_engulfing(
        self,
        min_body_ratio: float = 1.0,
        vol_mult: float = 0.0,
        min_body_pct: float = 0.001,
    ) -> bool:
        prev = self.k(1)
        curr = self.k(0)
        if prev.body <= 0 or curr.body <= 0:
            return False
        if prev.body_pct < min_body_pct or curr.body_pct < min_body_pct:
            return False
        if curr.body / prev.body < min_body_ratio:
            return False
        if vol_mult > 0 and curr.volume < prev.volume * vol_mult:
            return False
        return (
            prev.is_bull
            and curr.is_bear
            and curr.open >= prev.close
            and curr.close <= prev.open
            and (curr.open > prev.close or curr.close < prev.open)
        )

    def hammer(self, shadow_ratio: float = 2.0, max_upper_ratio: float = 0.5, min_body_pct: float = 0.0005) -> bool:
        curr = self.k(0)
        if curr.body <= 0 or curr.body_pct < min_body_pct:
            return False
        return curr.lower_shadow >= curr.body * shadow_ratio and curr.upper_shadow <= curr.body * max_upper_ratio

    def shooting_star(self, shadow_ratio: float = 2.0, max_lower_ratio: float = 0.5, min_body_pct: float = 0.0005) -> bool:
        curr = self.k(0)
        if curr.body <= 0 or curr.body_pct < min_body_pct:
            return False
        return curr.upper_shadow >= curr.body * shadow_ratio and curr.lower_shadow <= curr.body * max_lower_ratio

    def inside_bar(self) -> bool:
        prev = self.k(1)
        curr = self.k(0)
        return curr.high <= prev.high and curr.low >= prev.low and (curr.high < prev.high or curr.low > prev.low)

    def three_white_soldiers(self) -> bool:
        k2, k1, k0 = self.k(2), self.k(1), self.k(0)
        return (
            k2.is_bull
            and k1.is_bull
            and k0.is_bull
            and k1.close > k2.close
            and k0.close > k1.close
        )

    def three_black_crows(self) -> bool:
        k2, k1, k0 = self.k(2), self.k(1), self.k(0)
        return (
            k2.is_bear
            and k1.is_bear
            and k0.is_bear
            and k1.close < k2.close
            and k0.close < k1.close
        )

    def functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "k": self.k,
            "ma": self.ma,
            "sma": self.sma,
            "ema": self.ema,
            "vol_ma": self.vol_ma,
            "rsi": self.rsi,
            "highest_high": self.highest_high,
            "lowest_low": self.lowest_low,
            "highest_close": self.highest_close,
            "lowest_close": self.lowest_close,
            "bullish_engulfing": self.bullish_engulfing,
            "bearish_engulfing": self.bearish_engulfing,
            "hammer": self.hammer,
            "shooting_star": self.shooting_star,
            "inside_bar": self.inside_bar,
            "three_white_soldiers": self.three_white_soldiers,
            "three_black_crows": self.three_black_crows,
            "abs": abs,
            "min": min,
            "max": max,
        }


def _eval_node(node: ast.AST, ctx: _RuleEvalContext, funcs: dict[str, Callable[..., Any]]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx, funcs)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "curr":
            return ctx.k(0)
        if node.id == "prev":
            return ctx.k(1)
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        raise UnsafeExpressionError(f"未知变量：{node.id}")
    if isinstance(node, ast.Attribute):
        target = _eval_node(node.value, ctx, funcs)
        if not isinstance(target, CandleView):
            raise UnsafeExpressionError("仅允许访问 K 线对象的属性")
        if node.attr not in ALLOWED_CANDLE_ATTRS:
            raise UnsafeExpressionError(f"不允许的属性：{node.attr}")
        return getattr(target, node.attr)
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        fn = funcs[node.func.id]
        args = [_eval_node(arg, ctx, funcs) for arg in node.args]
        kwargs = {kw.arg: _eval_node(kw.value, ctx, funcs) for kw in node.keywords if kw.arg}
        return fn(*args, **kwargs)
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, ctx, funcs)
        if isinstance(node.op, ast.Not):
            return not bool(val)
        if isinstance(node.op, ast.USub):
            return -val
        if isinstance(node.op, ast.UAdd):
            return +val
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, ctx, funcs)
        right = _eval_node(node.right, ctx, funcs)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                return 0.0
            return left / right
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for v in node.values:
                if not bool(_eval_node(v, ctx, funcs)):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for v in node.values:
                if bool(_eval_node(v, ctx, funcs)):
                    return True
            return False
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx, funcs)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ctx, funcs)
            ok = False
            if isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            if not ok:
                return False
            left = right
        return True
    raise UnsafeExpressionError(f"不支持的运算节点：{type(node).__name__}")


def evaluate_expression(expression: str, closed_klines: list[Kline]) -> bool:
    """Safely evaluate a validated rule expression on ordered (oldest->newest) closed klines."""
    tree = validate_expression(expression)
    views = [CandleView(k) for k in closed_klines]
    ctx = _RuleEvalContext(views)
    return bool(_eval_node(tree, ctx, ctx.functions()))


def backtest_expression(expression: str, closed_klines: list[Kline], *, min_lookback: int = 2) -> list[Kline]:
    """Run a rule expression across historical closed candles and return all triggered candles."""
    tree = validate_expression(expression)
    views = [CandleView(k) for k in closed_klines]
    hits: list[Kline] = []
    start_idx = max(1, min_lookback - 1)
    for end_idx in range(start_idx, len(views)):
        ctx = _RuleEvalContext(views[: end_idx + 1])
        try:
            if bool(_eval_node(tree, ctx, ctx.functions())):
                hits.append(closed_klines[end_idx])
        except InsufficientDataError:
            continue
    return hits

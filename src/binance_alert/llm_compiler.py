from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import LLMConfig
from .rule_engine import ALLOWED_INTERVALS, CustomRule, UnsafeExpressionError, validate_expression

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个量化 K 线形态规则编译器。你的任务是把用户的自然语言监控需求，严格编译为可执行的 JSON 规则对象（只输出 JSON，不要输出任何额外文字或 Markdown 代码块）。

## 可用表达式语法（Python 布尔表达式安全子集）
- `curr`：最新一根已收盘 K 线；`prev`：前一根已收盘 K 线；`k(n)`：倒数第 n 根已收盘 K 线（`k(0)` 等价于 `curr`，`k(1)` 等价于 `prev`）。
- K 线对象只允许访问这些属性：
  `open`, `high`, `low`, `close`, `volume`, `body`, `body_pct`, `change_pct`, `range`, `upper_shadow`, `lower_shadow`, `is_bull`, `is_bear`
  （注意：`body_pct` 是小数，如 0.02 表示 2%；`change_pct` 是百分比数值，如 2.5 表示 +2.5%，-3.0 表示 -3.0%）。
- 内置技术指标函数（均基于已收盘 K 线，offset=0 表示截止最新已收盘，offset=1 表示截止前一根）：
  - `ma(period, offset=0)` / `sma(period, offset=0)`
  - `ema(period, offset=0)`
  - `vol_ma(period, offset=0)`
  - `rsi(period=14, offset=0)`
  - `highest_high(period, offset=0)` / `lowest_low(period, offset=0)`
  - `highest_close(period, offset=0)` / `lowest_close(period, offset=0)`
- 内置经典形态函数（返回布尔值，可直接用 `and`/`or`/`not` 组合）：
  - `bullish_engulfing(min_body_ratio=1.0, vol_mult=0.0, min_body_pct=0.001)`（阳包阴，vol_mult=1.5 表示放量 1.5 倍）
  - `bearish_engulfing(min_body_ratio=1.0, vol_mult=0.0, min_body_pct=0.001)`（阴包阳）
  - `hammer(shadow_ratio=2.0, max_upper_ratio=0.5, min_body_pct=0.0005)`（长下影锤头线）
  - `shooting_star(shadow_ratio=2.0, max_lower_ratio=0.5, min_body_pct=0.0005)`（长上影流星线）
  - `inside_bar()`（孕线）
  - `three_white_soldiers()`（红三兵/三连阳递增）
  - `three_black_crows()`（三只乌鸦/三连阴递减）
- 内置数学函数：`abs`, `min`, `max`。
- 禁止使用循环、列表推导、切片、赋值、import 或未列出的函数/属性。

## 输出 JSON Schema
{
  "name": "简短中文规则名（4~16字）",
  "description": "一句清晰的中文条件解释",
  "symbols": ["*"],  // 若用户未指定具体标的则填 ["*"]（表示全部监控标的）；若指定了标的则填原始名称如 ["NVDA", "BTC"]
  "interval": "1h",  // 可选：1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,1w；未指定则使用默认周期
  "lookback": 20,    // 计算该表达式至少需要多少根已收盘 K 线（2~200，例如涉及 ma(20) 至少填 25，rsi(14) 至少填 30）
  "expression": "合法的布尔表达式字符串"
}

## 示例
1) "NVDA 和 BTC 1小时线放量1.5倍阳包阴" ->
{"name":"放量1.5倍阳包阴","description":"1小时已收盘出现阳包阴且成交量至少为前一根的1.5倍","symbols":["NVDA","BTC"],"interval":"1h","lookback":5,"expression":"bullish_engulfing(min_body_ratio=1.0, vol_mult=1.5)"}

2) "ETH 15分钟线长下影锤头线且站上MA20" ->
{"name":"锤头线站上MA20","description":"15分钟已收盘出现下影线≥实体2倍的锤头线且收盘价高于20周期均线","symbols":["ETH"],"interval":"15m","lookback":25,"expression":"hammer(shadow_ratio=2.0) and curr.close > ma(20)"}

3) "放量突破近20根新高" ->
{"name":"放量突破20周期新高","description":"收盘价突破此前20根K线最高价且成交量大于20周期均量的1.5倍","symbols":["*"],"interval":"1h","lookback":25,"expression":"curr.close > highest_high(20, 1) and curr.volume > vol_ma(20, 1) * 1.5"}
"""


_ROUTER_SYSTEM_PROMPT = """你是企业微信内部群「币安 K 线形态监控助手」的意图路由器。
群成员不需要记任何命令，会直接用自然语言和机器人对话。请根据用户的自然语言和当前上下文，输出严格的 JSON（不要输出任何额外文字或代码块）。

## 输出 JSON Schema
{
  "actions": [
    // 按执行顺序列出 1~5 个动作对象，每个对象包含 "action" 和对应参数
  ]
}

## 支持的动作类型
1. `{"action": "status"}` — 查看机器人运行状态、推送通道、周期等
2. `{"action": "list_symbols"}` — 查看当前正在监控哪些标的
3. `{"action": "add_symbols", "symbols": ["MSFT", "BTC"]}` — 添加监控标的（请把中文名转成英文代码：英伟达->NVDA、特斯拉->TSLA、苹果->AAPL、微软->MSFT、谷歌->GOOGL、亚马逊->AMZN、比特币->BTC、以太坊->ETH、索拉纳->SOL、狗狗币->DOGE 等）
4. `{"action": "remove_symbols", "symbols": ["AAPLBUSDT"]}` — 删除监控标的（优先匹配上下文中已有监控列表里的精确 symbol）
5. `{"action": "check", "symbols": ["NVDA", "BTC"]}` — 立即查看某些标的的最新已收盘 K 线行情与形态（用户没指定具体标的时 symbols 传 `[]`）
6. `{"action": "history", "limit": 5}` — 查看最近触发过的告警历史
7. `{"action": "pause"}` — 暂停告警推送（如“先别推了”“静音一下”）
8. `{"action": "resume"}` — 恢复告警推送（如“重新打开推送”“继续提醒”）
9. `{"action": "list_rules"}` — 查看当前所有自定义形态规则
10. `{"action": "create_rule", "prompt": "用户的原始完整形态需求描述"}` — 新增一条 K 线形态/技术指标/价格触发规则
11. `{"action": "confirm_rule"}` — 仅当上下文 `has_pending_draft=true` 且用户表达同意/确认/激活（如“好的”“行”“确认”“开启吧”）时使用
12. `{"action": "cancel_rule"}` — 仅当上下文 `has_pending_draft=true` 且用户表达取消/不要了/放弃时使用
13. `{"action": "delete_rule", "rule_id": 1}` — 删除某条自定义规则（根据上下文规则列表匹配 rule_id）
14. `{"action": "set_rule_enabled", "rule_id": 1, "enabled": false}` — 启用(`true`)或停用(`false`)某条自定义规则
15. `{"action": "help"}` — 用户问“怎么用”“你能做什么”“有哪些功能”
16. `{"action": "chat", "reply": "简短友好的中文回复"}` — 闲聊、无法归类的提问，或缺少必要参数时礼貌引导

## 重要原则
- 支持一句话包含多个意图（例如“把苹果删了，顺便看下英伟达和比特币” -> 先 `remove_symbols`，再 `check`）。
- 如果用户说“监控 XXX”（只给标的名、没给新形态条件），用 `add_symbols`；如果给了具体形态/指标/价格条件（如“监控 ETH 15分钟跌破 MA20”），用 `create_rule`。
- 切勿编造不存在的 `rule_id`；如果找不到用户指的规则，用 `chat` 提示先看规则列表。
"""


class RuleCompileError(ValueError):
    """Raised when the LLM fails to compile a valid deterministic rule."""


class DeepSeekRuleCompiler:
    """Compiles natural-language pattern descriptions into validated CustomRule DSL via DeepSeek."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        kwargs: dict[str, Any] = {
            "base_url": config.base_url.rstrip("/"),
            "timeout": httpx.Timeout(config.timeout_seconds),
            "headers": {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    @property
    def is_ready(self) -> bool:
        return self.config.is_ready

    async def close(self) -> None:
        await self._client.aclose()

    def _chat_endpoint(self) -> str:
        # Handle both https://api.deepseek.com and https://api.deepseek.com/v1
        if self.config.base_url.rstrip("/").endswith("/v1"):
            return "/chat/completions"
        return "/chat/completions"

    async def _call_llm(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        resp = await self._client.post(self._chat_endpoint(), json=payload)
        if resp.status_code >= 400:
            raise RuleCompileError(f"DeepSeek API 返回错误 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuleCompileError("DeepSeek API 返回为空（无 choices）")
        content = (choices[0].get("message") or {}).get("content") or ""
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleCompileError(f"模型返回的不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise RuleCompileError("模型返回的 JSON 必须是对象")
        return parsed

    def _parse_and_validate(
        self,
        raw: dict[str, Any],
        *,
        prompt: str,
        default_interval: str,
        created_by: str,
    ) -> CustomRule:
        name = str(raw.get("name") or "").strip() or "自定义形态"
        description = str(raw.get("description") or "").strip() or prompt
        interval = str(raw.get("interval") or default_interval).strip()
        if interval not in ALLOWED_INTERVALS:
            raise RuleCompileError(f"不支持的 K 线周期：`{interval}`（支持：{', '.join(sorted(ALLOWED_INTERVALS))}）")

        raw_symbols = raw.get("symbols")
        if not raw_symbols or not isinstance(raw_symbols, list):
            symbols = ["*"]
        else:
            symbols = [str(s).strip().upper() for s in raw_symbols if str(s).strip()]
            if not symbols or "*" in symbols:
                symbols = ["*"]

        expression = str(raw.get("expression") or "").strip()
        validate_expression(expression)

        try:
            lookback = int(raw.get("lookback", 20))
        except (TypeError, ValueError):
            lookback = 20
        lookback = max(2, min(200, lookback))

        return CustomRule(
            id=None,
            name=name[:40],
            description=description[:200],
            symbols=symbols,
            interval=interval,
            expression=expression,
            lookback=lookback,
            enabled=True,
            source_prompt=prompt,
            created_by=created_by,
        )

    async def compile_rule(
        self,
        prompt: str,
        *,
        default_interval: str = "1h",
        watched_symbols: list[str] | None = None,
        created_by: str = "user",
    ) -> CustomRule:
        if not self.is_ready:
            raise RuleCompileError("未配置 DEEPSEEK_API_KEY，请先在 .env 中填入后再使用自然语言设形态。")

        user_msg = (
            f"默认周期：{default_interval}\n"
            f"当前已监控标的：{', '.join(watched_symbols or []) or '（暂无）'}\n"
            f"用户需求：{prompt}"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        last_err: Exception | None = None
        for attempt in range(2):
            raw = await self._call_llm(messages)
            try:
                return self._parse_and_validate(
                    raw,
                    prompt=prompt,
                    default_interval=default_interval,
                    created_by=created_by,
                )
            except (UnsafeExpressionError, RuleCompileError) as exc:
                last_err = exc
                logger.warning("LLM compiled invalid rule (attempt %d/2): %s", attempt + 1, exc)
                messages.append({"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)})
                messages.append(
                    {
                        "role": "user",
                        "content": f"你生成的规则校验失败：{exc}。请修正并重新仅输出合法 JSON。",
                    }
                )
        raise RuleCompileError(f"无法将描述编译为合法规则：{last_err}")

    async def route_intent(
        self,
        user_text: str,
        *,
        watched_symbols: list[tuple[str, str]],
        rules: list[CustomRule],
        pending_draft: CustomRule | None = None,
    ) -> list[dict[str, Any]]:
        """Parse free-form natural language into 1..5 structured bot actions via DeepSeek."""
        if not self.is_ready:
            return []

        symbols_ctx = (
            ", ".join(f"{sym}({name})" for sym, name in watched_symbols)
            if watched_symbols
            else "（空）"
        )
        rules_ctx = (
            "\n".join(
                f"#{r.id} {r.name} | {'启用' if r.enabled else '停用'} | {r.interval} | 标的:{','.join(r.symbols)} | {r.description}"
                for r in rules
            )
            if rules
            else "（暂无自定义规则）"
        )
        draft_ctx = (
            f"true（待确认规则：{pending_draft.name}，周期 {pending_draft.interval}，标的 {','.join(pending_draft.symbols)}）"
            if pending_draft is not None
            else "false"
        )

        user_msg = (
            f"当前监控标的：{symbols_ctx}\n"
            f"当前自定义规则：\n{rules_ctx}\n"
            f"has_pending_draft：{draft_ctx}\n"
            f"用户消息：{user_text}"
        )
        messages = [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        raw = await self._call_llm(messages)
        actions = raw.get("actions")
        if not isinstance(actions, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in actions[:5]:
            if isinstance(item, dict) and isinstance(item.get("action"), str):
                cleaned.append(item)
        return cleaned

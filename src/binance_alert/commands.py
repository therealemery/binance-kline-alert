from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .binance_client import BinanceClient, SymbolNotFoundError
from .config import AppConfig
from .detector import EngulfingDetector
from .formatter import INTERVAL_LABELS, format_kline_summary, format_timestamp
from .llm_compiler import DeepSeekRuleCompiler, RuleCompileError
from .models import PatternType
from .rule_engine import CustomRule, backtest_expression
from .store import StateStore

logger = logging.getLogger(__name__)

# Matches leading @BotName mentions in WeCom/Telegram group chats (handles space or \u2005 four-per-em space)
_LEADING_MENTION_RE = re.compile(r"^(?:@[^\s\u2005]+[\s\u2005]+)+")
_SLASH_CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?=\s|$)")


def strip_mentions(text: str) -> str:
    cleaned = text.replace("\u2005", " ").strip()
    cleaned = _LEADING_MENTION_RE.sub("", cleaned).strip()
    cleaned = _SLASH_CMD_RE.sub(r"\1", cleaned).strip()
    return cleaned


@dataclass
class _PendingRuleDraft:
    rule: CustomRule
    created_at: float


class CommandHandler:
    """Handles both fast-path slash/keyword commands and full natural-language requests via DeepSeek."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: StateStore,
        binance: BinanceClient,
        detector: EngulfingDetector,
        llm_compiler: DeepSeekRuleCompiler | None = None,
        status_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.binance = binance
        self.detector = detector
        self.llm_compiler = llm_compiler
        self.status_provider = status_provider
        self._pending_drafts: dict[str, _PendingRuleDraft] = {}

    def _is_authorized(self, user_id: str, *, channel: str = "wecom") -> bool:
        allowed = (
            self.config.telegram.allowed_users
            if channel == "telegram"
            else self.config.wecom.aibot.allowed_users
        )
        if not allowed:
            return True
        return bool(user_id) and user_id in allowed

    def _draft_key(self, user_id: str, channel: str = "wecom") -> str:
        return f"{channel}:{user_id or '__default__'}"

    def _get_active_draft(self, user_id: str, *, channel: str = "wecom") -> CustomRule | None:
        key = self._draft_key(user_id, channel)
        draft = self._pending_drafts.get(key)
        if not draft:
            return None
        if (time.monotonic() - draft.created_at) > 300:
            self._pending_drafts.pop(key, None)
            return None
        return draft.rule

    async def handle(self, raw_text: str, *, user_id: str = "", channel: str = "wecom") -> str:
        text = strip_mentions(raw_text)
        if not text:
            return self._help_text()

        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        # ---- Fast-path exact commands (0ms, no LLM call) ----
        if cmd in {"help", "帮助", "菜单", "?", "？", "start"}:
            return self._help_text()

        if cmd in {"status", "状态"} and not args:
            return self._handle_status()

        if cmd in {"list", "列表", "标的"} and not args:
            return self._handle_list()

        if cmd in {"rules", "规则", "规则列表", "形态列表"} and not args:
            return self._handle_rules()

        if cmd in {"confirm", "确认", "确定", "激活"} and not args:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限激活自定义规则。"
            return self._handle_confirm_rule(user_id=user_id, channel=channel)

        if cmd in {"cancel", "取消", "放弃"} and not args:
            return self._handle_cancel_rule(user_id=user_id, channel=channel)

        if cmd in {"rule", "addrule", "新增规则", "添加规则", "设置形态"}:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限创建自定义规则。"
            prompt = text[len(parts[0]):].strip()
            return await self._handle_compile_rule(prompt, user_id=user_id, channel=channel)

        if cmd in {"delrule", "rmrule", "删除规则", "移除规则"}:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限删除自定义规则。"
            return self._handle_delete_rule(args)

        if cmd in {"togglerule", "开关规则", "启停规则"}:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限切换规则状态。"
            return self._handle_toggle_rule(args)

        if cmd in {"history", "历史", "记录"} and len(args) <= 1:
            limit = 5
            if args:
                try:
                    limit = max(1, min(int(args[0]), 20))
                except ValueError:
                    limit = 5
            return self._handle_history(limit)

        if cmd in {"check", "检查", "行情"}:
            return await self._handle_check(args)

        if cmd in {"add", "添加", "新增"} and args:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限修改监控列表（未在 `allowed_users` 白名单中）。"
            return await self._handle_add(args, user_id=user_id, channel=channel)

        if cmd in {"remove", "rm", "del", "delete", "删除", "移除"} and args:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限修改监控列表（未在 `allowed_users` 白名单中）。"
            return self._handle_remove(args)

        if cmd in {"pause", "暂停"} and not args:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限暂停提醒。"
            self.store.set_paused(True)
            return "⏸️ 已暂停告警推送（监控仍保持在线，说一声「恢复推送」即可重新开启）。"

        if cmd in {"resume", "恢复", "开启"} and not args:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限恢复提醒。"
            self.store.set_paused(False)
            return "▶️ 已恢复告警推送。"

        # ---- Natural-language intent routing via DeepSeek-Flash ----
        if self.llm_compiler and self.llm_compiler.is_ready:
            return await self._handle_natural_language(text, user_id=user_id, channel=channel)

        return f"未识别命令：`{parts[0]}`\n\n{self._help_text()}"

    async def _handle_natural_language(self, text: str, *, user_id: str, channel: str = "wecom") -> str:
        watched = [(s.symbol, s.display_name) for s in self.store.list_symbols()]
        rules = self.store.list_rules(enabled_only=False)
        pending = self._get_active_draft(user_id, channel=channel)

        try:
            actions = await self.llm_compiler.route_intent(  # type: ignore[union-attr]
                text,
                watched_symbols=watched,
                rules=rules,
                pending_draft=pending,
            )
        except Exception as exc:
            logger.exception("Natural-language intent routing failed")
            return f"❌ 理解指令时出错：{exc}\n\n{self._help_text()}"

        if not actions:
            return f"没太听明白你的意思，可以换种说法试试～\n\n{self._help_text()}"

        outputs: list[str] = []
        for item in actions:
            outputs.append(
                await self._execute_routed_action(item, original_text=text, user_id=user_id, channel=channel)
            )

        return "\n\n---\n\n".join(o for o in outputs if o)

    async def _execute_routed_action(
        self,
        item: dict[str, Any],
        *,
        original_text: str,
        user_id: str,
        channel: str = "wecom",
    ) -> str:
        action = str(item.get("action") or "").strip().lower()

        if action == "status":
            return self._handle_status()
        if action == "list_symbols":
            return self._handle_list()
        if action == "list_rules":
            return self._handle_rules()
        if action == "help":
            return self._help_text()
        if action == "chat":
            reply = str(item.get("reply") or "").strip()
            return reply or self._help_text()
        if action == "history":
            try:
                limit = max(1, min(int(item.get("limit", 5)), 20))
            except (TypeError, ValueError):
                limit = 5
            return self._handle_history(limit)
        if action == "check":
            syms = [str(s).strip() for s in (item.get("symbols") or []) if str(s).strip()]
            return await self._handle_check(syms)

        # ---- Mutating actions require authorization ----
        if action == "add_symbols":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限修改监控列表（未在 `allowed_users` 白名单中）。"
            syms = [str(s).strip() for s in (item.get("symbols") or []) if str(s).strip()]
            return await self._handle_add(syms, user_id=user_id, channel=channel)

        if action == "remove_symbols":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限修改监控列表（未在 `allowed_users` 白名单中）。"
            syms = [str(s).strip() for s in (item.get("symbols") or []) if str(s).strip()]
            return self._handle_remove(syms)

        if action == "pause":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限暂停提醒。"
            self.store.set_paused(True)
            return "⏸️ 已暂停告警推送（说一声「恢复推送」即可重新开启）。"

        if action == "resume":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限恢复提醒。"
            self.store.set_paused(False)
            return "▶️ 已恢复告警推送。"

        if action == "create_rule":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限创建自定义规则。"
            prompt = str(item.get("prompt") or original_text).strip()
            return await self._handle_compile_rule(prompt, user_id=user_id, channel=channel)

        if action == "confirm_rule":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限激活自定义规则。"
            return self._handle_confirm_rule(user_id=user_id, channel=channel)

        if action == "cancel_rule":
            return self._handle_cancel_rule(user_id=user_id, channel=channel)

        if action == "delete_rule":
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限删除自定义规则。"
            rule_id = item.get("rule_id")
            return self._handle_delete_rule([str(rule_id)] if rule_id is not None else [])

        if action in {"set_rule_enabled", "toggle_rule"}:
            if not self._is_authorized(user_id, channel=channel):
                return "⛔ 你没有权限切换规则状态。"
            rule_id = item.get("rule_id")
            if rule_id is None:
                return "请告诉我需要开关哪条规则（可先问「现在有哪些规则」查看编号）。"
            try:
                rid = int(str(rule_id).lstrip("#"))
            except ValueError:
                return f"无效的规则编号：`{rule_id}`"
            enabled_val = item.get("enabled")
            if isinstance(enabled_val, bool):
                new_state = self.store.set_rule_enabled(rid, enabled_val)
            else:
                new_state = self.store.toggle_rule(rid)
            if new_state is None:
                return f"⚠️ 未找到规则 **#{rid}**。"
            return f"{'🟢 已启用' if new_state else '⚪ 已停用'}规则 **#{rid}**。"

        return self._help_text()

    def _help_text(self) -> str:
        return (
            "### 🤖 币安 K 线形态监控助手\n"
            "不用记任何命令，在群里 `@我` 直接说大白话就行，例如：\n"
            "- **看行情/状态**：「现在都在监控哪些标的？」「看看英伟达和比特币最新收盘怎么样」「最近触发过哪些信号？」\n"
            "- **增删标的**：「帮我加上微软和狗狗币」「把苹果删了吧」\n"
            "- **自定义形态**：「帮我加个监控，ETH 15分钟线出现长下影锤头线且站上 MA20 提醒我」「所有标的 4小时线放量突破近20根新高提醒」\n"
            "- **管规则/推送**：「现在有哪些规则」「把1号规则关掉」「删除2号规则」「先暂停推送」「恢复推送」"
        )

    def _handle_status(self) -> str:
        symbols = self.store.list_symbols()
        rules = self.store.list_rules(enabled_only=True)
        paused = self.store.is_paused()
        interval = self.config.binance.interval
        interval_label = INTERVAL_LABELS.get(interval, interval)
        last_poll = self.store.get_meta("last_poll_at") or "尚未轮询"
        total_alerts = self.store.get_total_alert_count()
        webhook_cnt = len(self.config.wecom.webhook_urls)

        extra_lines: list[str] = []
        if self.status_provider:
            for k, v in self.status_provider().items():
                extra_lines.append(f"> **{k}**：{v}")
        extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

        vol_filter = "开启" if self.config.alert.enable_volume_filter else "关闭"
        state_text = "⏸️ 已暂停推送" if paused else "✅ 运行中"
        llm_status = f"🟢 {self.config.llm.model}" if self.config.llm.is_ready else "⚪ 未配置 API Key"

        return (
            "### 📊 监控运行状态\n"
            f"> **状态**：{state_text}\n"
            f"> **默认周期 / 轮询**：{interval_label}（`{interval}`） / 每 {self.config.binance.poll_interval_seconds}s\n"
            f"> **监控标的数**：{len(symbols)} 个\n"
            f"> **启用自定义规则**：{len(rules)} 条\n"
            f"> **自然语言理解**：{llm_status}\n"
            f"> **默认吞没过滤**：成交量放大={vol_filter}，最小实体比=`{self.config.alert.min_body_ratio}`\n"
            f"> **Webhook 通道**：{webhook_cnt} 个\n"
            f"> **累计推送次数**：{total_alerts}\n"
            f"> **最近轮询**：{last_poll}"
            f"{extra_block}"
        )

    def _handle_list(self) -> str:
        symbols = self.store.list_symbols()
        if not symbols:
            return "当前没有监控标的。直接说「帮我加上 BTC 和 NVDA」即可新增。"
        lines = [f"### 📋 当前监控列表（共 {len(symbols)} 个）"]
        for idx, item in enumerate(symbols, 1):
            lines.append(f"{idx}. **{item.display_name}** (`{item.symbol}`)")
        return "\n".join(lines)

    def _handle_rules(self) -> str:
        rules = self.store.list_rules(enabled_only=False)
        if not rules:
            return (
                "当前还没有自定义规则（默认内置的 1h 阳包阴/阴包阳正在运行）。\n"
                "直接说即可添加，例如：「NVDA 和 BTC 1小时线放量1.5倍阳包阴提醒我」"
            )
        lines = [f"### 🧩 自定义形态规则（共 {len(rules)} 条）"]
        for r in rules:
            status_icon = "🟢" if r.enabled else "⚪"
            sym_str = "全部监控标的" if r.symbols == ["*"] else ", ".join(r.symbols)
            lines.append(
                f"- {status_icon} **#{r.id} {r.name}** | 周期 `{r.interval}` | 标的：{sym_str}\n"
                f"  说明：{r.description}\n"
                f"  公式：`{r.expression}`"
            )
        return "\n".join(lines)

    async def _handle_compile_rule(self, prompt: str, *, user_id: str, channel: str = "wecom") -> str:
        if not prompt:
            return (
                "请直接用自然语言描述你想监控的形态，例如：\n"
                "- 「NVDA 和 BTC 1小时线放量1.5倍阳包阴」\n"
                "- 「ETH 15分钟线长下影锤头线且站上MA20」\n"
                "- 「所有标的放量突破近20根新高」"
            )
        if not self.llm_compiler or not self.llm_compiler.is_ready:
            return "⚠️ 尚未配置 `DEEPSEEK_API_KEY`，请在 `.env` 文件中填写后再使用自然语言设形态。"

        watched = [s.symbol for s in self.store.list_symbols()]
        try:
            draft = await self.llm_compiler.compile_rule(
                prompt,
                default_interval=self.config.binance.interval,
                watched_symbols=watched,
                created_by=user_id or channel,
            )
        except RuleCompileError as exc:
            return f"❌ 规则编译失败：{exc}"
        except Exception as exc:
            logger.exception("Unexpected error compiling rule")
            return f"❌ 调用 `{self.config.llm.model}` 失败：{exc}"

        resolved_symbols: list[str] = []
        if draft.symbols == ["*"]:
            resolved_symbols = ["*"]
        else:
            for raw_sym in draft.symbols:
                try:
                    sym, _ = await self.binance.resolve_symbol(raw_sym)
                    if sym not in resolved_symbols:
                        resolved_symbols.append(sym)
                except SymbolNotFoundError as exc:
                    return f"❌ 编译出标的 `{raw_sym}`，但校验失败：{exc}"

        draft = CustomRule(
            id=None,
            name=draft.name,
            description=draft.description,
            symbols=resolved_symbols,
            interval=draft.interval,
            expression=draft.expression,
            lookback=draft.lookback,
            enabled=True,
            source_prompt=draft.source_prompt,
            created_by=draft.created_by,
        )

        sample_symbols = watched[:2] if resolved_symbols == ["*"] else resolved_symbols[:2]
        backtest_lines: list[str] = []
        fetch_count = max(self.config.llm.backtest_candles, draft.lookback + 20)
        for sym in sample_symbols:
            try:
                closed = await self.binance.get_closed_klines(sym, draft.interval, count=fetch_count)
                hits = backtest_expression(draft.expression, closed, min_lookback=draft.lookback)
                latest_str = (
                    f"，最近触发：{format_timestamp(hits[-1].open_time, self.config.alert.timezone)}"
                    if hits
                    else ""
                )
                backtest_lines.append(f"- `{sym}` 最近 {len(closed)} 根已收盘 K 线触发 **{len(hits)} 次**{latest_str}")
            except Exception as exc:
                backtest_lines.append(f"- `{sym}` 回测预览失败：{exc}")

        self._pending_drafts[self._draft_key(user_id, channel)] = _PendingRuleDraft(
            rule=draft,
            created_at=time.monotonic(),
        )

        sym_label = "全部监控标的" if draft.symbols == ["*"] else ", ".join(f"`{s}`" for s in draft.symbols)
        interval_label = INTERVAL_LABELS.get(draft.interval, draft.interval)
        bt_block = "\n".join(backtest_lines) if backtest_lines else "- （当前监控列表为空，跳过历史预览）"

        return (
            f"### 🧠 规则编译预览（`{self.config.llm.model}`）\n"
            f"> **名称**：{draft.name}\n"
            f"> **标的**：{sym_label}\n"
            f"> **周期**：{interval_label}（`{draft.interval}`）\n"
            f"> **说明**：{draft.description}\n"
            f"> **确定性公式**：`{draft.expression}`\n\n"
            f"**历史回测预览**：\n{bt_block}\n\n"
            "请回复「确认」（或「好的，开启吧」）激活该规则，或回复「取消」放弃（5 分钟内有效）。"
        )

    def _handle_confirm_rule(self, *, user_id: str, channel: str = "wecom") -> str:
        draft = self._get_active_draft(user_id, channel=channel)
        if draft is None:
            return "当前没有待确认的规则草稿（或已超过 5 分钟过期），请重新告诉我你想加什么规则。"

        rule_id = self.store.add_rule(draft)
        self._pending_drafts.pop(self._draft_key(user_id, channel), None)
        sym_label = "全部监控标的" if draft.symbols == ["*"] else ", ".join(f"`{s}`" for s in draft.symbols)
        return (
            f"✅ 规则 **#{rule_id} {draft.name}** 已激活！\n"
            f"> 标的：{sym_label} | 周期：`{draft.interval}`\n"
            f"> 公式：`{draft.expression}`\n"
            "后续将由服务器本地确定性引擎自动监控（零 LLM 消耗）。"
        )

    def _handle_cancel_rule(self, *, user_id: str, channel: str = "wecom") -> str:
        key = self._draft_key(user_id, channel)
        if key in self._pending_drafts:
            self._pending_drafts.pop(key, None)
            return "已取消待确认的规则草稿。"
        return "当前没有待确认的规则草稿。"

    def _handle_delete_rule(self, args: list[str]) -> str:
        if not args:
            return "请指定要删除的规则编号，例如：「删除 1 号规则」（可先问「现在有哪些规则」查看编号）。"
        try:
            rule_id = int(args[0].lstrip("#"))
        except ValueError:
            return f"无效的规则编号：`{args[0]}`"
        if self.store.delete_rule(rule_id):
            return f"🗑️ 已删除规则 **#{rule_id}**。"
        return f"⚠️ 未找到规则 **#{rule_id}**。"

    def _handle_toggle_rule(self, args: list[str]) -> str:
        if not args:
            return "请指定规则编号，例如：「把 1 号规则关掉」。"
        try:
            rule_id = int(args[0].lstrip("#"))
        except ValueError:
            return f"无效的规则编号：`{args[0]}`"
        new_state = self.store.toggle_rule(rule_id)
        if new_state is None:
            return f"⚠️ 未找到规则 **#{rule_id}**。"
        return f"{'🟢 已启用' if new_state else '⚪ 已停用'}规则 **#{rule_id}**。"

    def _handle_history(self, limit: int) -> str:
        rows = self.store.get_recent_history(limit=limit)
        if not rows:
            return "暂无历史触发记录。"
        lines = [f"### 🕒 最近 {len(rows)} 条形态触发记录"]
        for row in rows:
            raw_pattern = str(row["pattern"])
            if raw_pattern.startswith("rule:"):
                parts = raw_pattern.split(":", 2)
                label = f"🎯 **{parts[2] if len(parts) > 2 else raw_pattern}**"
            else:
                try:
                    pattern = PatternType(raw_pattern)
                    label = f"{pattern.emoji} **{pattern.cn_name}**"
                except ValueError:
                    label = f"🎯 **{raw_pattern}**"
            ts = format_timestamp(int(row["open_time"]), self.config.alert.timezone)
            pct = float(row["change_pct"])
            lines.append(
                f"- `{row['symbol']}` ({row['interval']}) {label} | {ts} | "
                f"`{row['curr_open']}` → `{row['curr_close']}` (`{pct:+.2f}%`)"
            )
        return "\n".join(lines)

    async def _handle_add(self, args: list[str], *, user_id: str, channel: str = "wecom") -> str:
        if not args:
            return "请告诉我要添加哪些标的，例如：「帮我加上比特币和微软」"

        results: list[str] = []
        for raw in args:
            try:
                symbol, display_name = await self.binance.resolve_symbol(raw)
            except SymbolNotFoundError as exc:
                results.append(f"- ❌ {exc}")
                continue
            except Exception as exc:
                logger.exception("Failed resolving symbol %s", raw)
                results.append(f"- ❌ 查询 `{raw}` 失败：{exc}")
                continue

            created = self.store.add_symbol(
                symbol,
                display_name=display_name,
                added_by=user_id or channel,
            )
            if created:
                results.append(f"- ✅ 已添加 **{display_name}** (`{symbol}`)")
            else:
                results.append(f"- ℹ️ **{display_name}** (`{symbol}`) 已在监控列表中")

        return "### ➕ 添加标的\n" + "\n".join(results)

    def _handle_remove(self, args: list[str]) -> str:
        if not args:
            return "请告诉我要移除哪个标的，例如：「把苹果删了」"

        results: list[str] = []
        for raw in args:
            removed = self.store.remove_symbol(raw)
            if removed:
                results.append(f"- 🗑️ 已移除 `{removed}`")
            else:
                results.append(f"- ⚠️ 监控列表中未找到 `{raw}`")
        return "### ➖ 移除标的\n" + "\n".join(results)

    async def _handle_check(self, args: list[str]) -> str:
        interval = self.config.binance.interval
        targets: list[tuple[str, str]] = []

        if args:
            for raw in args[:5]:
                try:
                    sym, name = await self.binance.resolve_symbol(raw)
                    targets.append((sym, name))
                except SymbolNotFoundError as exc:
                    return f"❌ {exc}"
                except Exception as exc:
                    return f"❌ 查询 `{raw}` 失败：{exc}"
        else:
            watched = self.store.list_symbols()
            if not watched:
                return "当前监控列表为空，请先添加标的或直接说「看看 BTC 现在怎么样」。"
            targets = [(w.symbol, w.display_name) for w in watched[:5]]

        blocks: list[str] = [f"### 🔍 最新已收盘 {INTERVAL_LABELS.get(interval, interval)} K 线检查"]
        for sym, name in targets:
            try:
                prev_k, curr_k = await self.binance.get_last_two_closed_klines(sym, interval)
                sig = self.detector.detect(prev_k, curr_k, display_name=name)
                blocks.append(
                    format_kline_summary(sym, name, prev_k, curr_k, sig, self.config.alert.timezone)
                )
            except Exception as exc:
                blocks.append(f"**{name}** (`{sym}`)\n- ❌ 拉取失败：{exc}")

        if not args and len(self.store.list_symbols()) > 5:
            blocks.append("_（仅展示前 5 个标的，可直接说「看看 <标的>」查看单个标的）_")
        return "\n\n".join(blocks)

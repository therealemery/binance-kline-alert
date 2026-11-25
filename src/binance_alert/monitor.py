from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .aibot_client import WeComAiBotBridge
from .binance_client import BinanceClient
from .config import AppConfig
from .detector import EngulfingDetector
from .formatter import format_custom_rule_alert_markdown, format_signal_markdown, infer_display_name
from .models import Kline, PatternSignal
from .rule_engine import CustomRule, InsufficientDataError, evaluate_expression
from .store import StateStore
from .telegram_client import TelegramBotClient
from .webhook import WeComWebhookClient

logger = logging.getLogger(__name__)


@dataclass
class CustomRuleHit:
    rule: CustomRule
    symbol: str
    display_name: str
    curr: Kline
    prev: Kline | None


@dataclass
class PollSummary:
    checked: int = 0
    signals: list[PatternSignal] = field(default_factory=list)
    rule_hits: list[CustomRuleHit] = field(default_factory=list)
    delivered: int = 0
    deduped: int = 0
    paused_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class KlineMonitor:
    """Periodic Binance K-line polling and alert dispatch engine (default engulfing + custom rules)."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: StateStore,
        binance: BinanceClient,
        detector: EngulfingDetector,
        webhook: WeComWebhookClient,
        aibot: WeComAiBotBridge | None = None,
        telegram: TelegramBotClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.binance = binance
        self.detector = detector
        self.webhook = webhook
        self.aibot = aibot
        self.telegram = telegram
        self._semaphore = asyncio.Semaphore(config.binance.max_concurrency)

    async def _fetch_series(
        self, symbol: str, interval: str, count: int
    ) -> tuple[tuple[str, str], list[Kline] | None, str | None]:
        async with self._semaphore:
            try:
                klines = await self.binance.get_closed_klines(symbol, interval, count=count)
                return (symbol, interval), klines, None
            except Exception as exc:
                logger.warning("Failed fetching %s (%s): %s", symbol, interval, exc)
                return (symbol, interval), None, f"{symbol}({interval}): {exc}"

    async def _dispatch_markdown(self, md: str) -> tuple[bool, bool, bool, bool]:
        """Deliver markdown to active WeCom & Telegram channels.

        Returns (webhook_ok, aibot_ok, tg_ok, has_channel).
        """
        aibot_active = (
            self.aibot is not None
            and self.config.wecom.aibot.push_alerts
            and self.aibot.is_connected
        )
        tg_active = self.telegram is not None and self.telegram.is_configured
        has_channel = self.webhook.is_configured or aibot_active or tg_active

        async def _send_webhook() -> bool:
            return await self.webhook.send_markdown(md) if self.webhook.is_configured else False

        async def _send_aibot() -> bool:
            if self.aibot is not None and self.config.wecom.aibot.push_alerts:
                return await self.aibot.push_markdown(md)
            return False

        async def _send_telegram() -> bool:
            return await self.telegram.send_markdown(md) if tg_active and self.telegram is not None else False

        webhook_ok, aibot_ok, tg_ok = await asyncio.gather(
            _send_webhook(),
            _send_aibot(),
            _send_telegram(),
        )
        return webhook_ok, aibot_ok, tg_ok, has_channel

    async def poll_once(self, *, dry_run: bool = False) -> PollSummary:
        watched_items = self.store.list_symbols(enabled_only=True)
        display_map = {item.symbol: item.display_name for item in watched_items}
        enabled_rules = self.store.list_rules(enabled_only=True)

        # Compute minimal set of (symbol, interval) -> max_required_count for this cycle
        fetch_plan: dict[tuple[str, str], int] = {}
        default_interval = self.config.binance.interval

        if self.config.alert.enable_default_engulfing:
            for item in watched_items:
                key = (item.symbol, default_interval)
                fetch_plan[key] = max(fetch_plan.get(key, 0), 2)

        rule_targets: list[tuple[CustomRule, list[str]]] = []
        for rule in enabled_rules:
            targets = [item.symbol for item in watched_items] if rule.symbols == ["*"] else rule.symbols
            rule_targets.append((rule, targets))
            for sym in targets:
                key = (sym, rule.interval)
                fetch_plan[key] = max(fetch_plan.get(key, 0), max(rule.lookback, 2))

        summary = PollSummary(checked=len(fetch_plan))
        if not fetch_plan:
            logger.debug("No symbols or rules to monitor")
            return summary

        fetch_results = await asyncio.gather(
            *(self._fetch_series(sym, iv, cnt) for (sym, iv), cnt in fetch_plan.items())
        )
        series_map: dict[tuple[str, str], list[Kline]] = {}
        for key, klines, err in fetch_results:
            if err is not None:
                summary.errors.append(err)
            elif klines is not None:
                series_map[key] = klines

        tz = ZoneInfo(self.config.alert.timezone)
        self.store.set_meta("last_poll_at", datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"))
        paused = self.store.is_paused()

        # 1. Default engulfing detector
        if self.config.alert.enable_default_engulfing:
            for item in watched_items:
                klines = series_map.get((item.symbol, default_interval))
                if not klines or len(klines) < 2:
                    continue
                signal = self.detector.detect(klines[-2], klines[-1], display_name=item.display_name)
                if signal is None:
                    continue

                summary.signals.append(signal)
                last_open = self.store.get_last_alert_open_time(signal.symbol, signal.interval)
                if last_open == signal.open_time:
                    summary.deduped += 1
                    continue

                if paused and not dry_run:
                    summary.paused_skipped += 1
                    logger.info("Signal %s %s skipped (paused)", signal.symbol, signal.pattern.cn_name)
                    continue

                md = format_signal_markdown(signal, self.config.alert.timezone)
                if dry_run:
                    logger.info("[DRY-RUN] Signal detected:\n%s", md)
                    continue

                webhook_ok, aibot_ok, tg_ok, has_channel = await self._dispatch_markdown(md)
                if webhook_ok or aibot_ok or tg_ok or not has_channel:
                    self.store.record_alert(
                        signal,
                        delivered_webhook=webhook_ok,
                        delivered_aibot=aibot_ok or tg_ok,
                    )
                    if webhook_ok or aibot_ok or tg_ok:
                        summary.delivered += 1
                        logger.info(
                            "Delivered %s %s alert (webhook=%s, aibot=%s, telegram=%s)",
                            signal.symbol,
                            signal.pattern.cn_name,
                            webhook_ok,
                            aibot_ok,
                            tg_ok,
                        )
                    else:
                        logger.warning(
                            "Signal %s %s detected but no alert push channel active:\n%s",
                            signal.symbol,
                            signal.pattern.cn_name,
                            md,
                        )
                else:
                    logger.error(
                        "All alert push channels failed for %s %s; will retry on next poll",
                        signal.symbol,
                        signal.pattern.cn_name,
                    )

        # 2. Custom LLM-compiled rules
        for rule, targets in rule_targets:
            assert rule.id is not None
            for sym in targets:
                klines = series_map.get((sym, rule.interval))
                if not klines:
                    continue
                try:
                    matched = evaluate_expression(rule.expression, klines)
                except InsufficientDataError as exc:
                    logger.debug("Rule #%s (%s %s) skipped: %s", rule.id, sym, rule.interval, exc)
                    continue
                except Exception as exc:
                    logger.warning("Rule #%s (%s) eval error: %s", rule.id, rule.name, exc)
                    summary.errors.append(f"rule#{rule.id}({sym}): {exc}")
                    continue

                if not matched:
                    continue

                curr = klines[-1]
                prev = klines[-2] if len(klines) >= 2 else None
                display_name = display_map.get(sym, infer_display_name(sym))
                summary.rule_hits.append(
                    CustomRuleHit(rule=rule, symbol=sym, display_name=display_name, curr=curr, prev=prev)
                )

                last_open = self.store.get_last_rule_alert_open_time(rule.id, sym, rule.interval)
                if last_open == curr.open_time:
                    summary.deduped += 1
                    continue

                if paused and not dry_run:
                    summary.paused_skipped += 1
                    logger.info("Rule #%s (%s) skipped (paused)", rule.id, sym)
                    continue

                md = format_custom_rule_alert_markdown(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    rule_description=rule.description,
                    symbol=sym,
                    display_name=display_name,
                    interval=rule.interval,
                    curr=curr,
                    prev=prev,
                    tz_name=self.config.alert.timezone,
                )
                if dry_run:
                    logger.info("[DRY-RUN] Custom rule hit:\n%s", md)
                    continue

                webhook_ok, aibot_ok, tg_ok, has_channel = await self._dispatch_markdown(md)
                if webhook_ok or aibot_ok or tg_ok or not has_channel:
                    self.store.record_rule_alert(
                        rule=rule,
                        symbol=sym,
                        curr=curr,
                        prev=prev,
                        delivered_webhook=webhook_ok,
                        delivered_aibot=aibot_ok or tg_ok,
                    )
                    if webhook_ok or aibot_ok or tg_ok:
                        summary.delivered += 1
                        logger.info(
                            "Delivered custom rule #%s (%s %s) alert (webhook=%s, aibot=%s, telegram=%s)",
                            rule.id,
                            sym,
                            rule.name,
                            webhook_ok,
                            aibot_ok,
                            tg_ok,
                        )
                    else:
                        logger.warning("Custom rule #%s hit but no alert push channel active:\n%s", rule.id, md)
                else:
                    logger.error("All alert push channels failed for custom rule #%s (%s); will retry", rule.id, sym)

        return summary

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "Starting KlineMonitor: interval=%s poll=%ds symbols=%d custom_rules=%d",
            self.config.binance.interval,
            self.config.binance.poll_interval_seconds,
            len(self.store.list_symbols()),
            len(self.store.list_rules(enabled_only=True)),
        )
        while not stop_event.is_set():
            try:
                summary = await self.poll_once(dry_run=False)
                logger.debug(
                    "Poll done: checked=%d signals=%d rule_hits=%d delivered=%d deduped=%d errors=%d",
                    summary.checked,
                    len(summary.signals),
                    len(summary.rule_hits),
                    summary.delivered,
                    summary.deduped,
                    len(summary.errors),
                )
            except Exception:
                logger.exception("Unexpected error during poll cycle")

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.config.binance.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .aibot_client import WeComAiBotBridge
from .binance_client import BinanceClient, SymbolNotFoundError
from .commands import CommandHandler
from .config import AppConfig, load_config
from .detector import EngulfingDetector
from .formatter import format_custom_rule_alert_markdown, format_signal_markdown, format_timestamp
from .llm_compiler import DeepSeekRuleCompiler, RuleCompileError
from .monitor import KlineMonitor
from .rule_engine import CustomRule, backtest_expression
from .store import StateStore
from .webhook import WeComWebhookClient

logger = logging.getLogger("binance_alert")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Prevent httpx INFO logs from leaking WeCom webhook keys in journald
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _build_components(cfg: AppConfig) -> tuple[
    StateStore,
    BinanceClient,
    EngulfingDetector,
    WeComWebhookClient,
    DeepSeekRuleCompiler,
    WeComAiBotBridge,
    KlineMonitor,
]:
    store = StateStore(cfg.storage.sqlite_path)
    seeded = store.seed_symbols_if_empty(cfg.binance.symbols)
    if seeded:
        logger.info("Seeded %d initial symbols from config into SQLite", seeded)

    binance = BinanceClient(
        base_url=cfg.binance.base_url,
        proxy=cfg.binance.proxy,
        timeout_seconds=cfg.binance.timeout_seconds,
    )
    detector = EngulfingDetector(
        enable_volume_filter=cfg.alert.enable_volume_filter,
        min_body_ratio=cfg.alert.min_body_ratio,
        min_body_pct=cfg.alert.min_body_pct,
    )
    webhook = WeComWebhookClient(cfg.wecom.webhook_urls)
    llm_compiler = DeepSeekRuleCompiler(cfg.llm)

    aibot_holder: list[WeComAiBotBridge] = []

    def _status_provider() -> dict[str, str]:
        ws_status = "未启用"
        if cfg.wecom.aibot.is_ready:
            ws_status = "🟢 已连接" if (aibot_holder and aibot_holder[0].is_connected) else "🟡 重连中"
        proxy_status = "已配置" if cfg.binance.proxy else "直连"
        return {
            "智能机器人长连接": ws_status,
            "币安网络": proxy_status,
        }

    cmd_handler = CommandHandler(
        config=cfg,
        store=store,
        binance=binance,
        detector=detector,
        llm_compiler=llm_compiler,
        status_provider=_status_provider,
    )
    aibot = WeComAiBotBridge(
        config=cfg.wecom.aibot,
        store=store,
        command_handler=cmd_handler,
    )
    aibot_holder.append(aibot)

    monitor = KlineMonitor(
        config=cfg,
        store=store,
        binance=binance,
        detector=detector,
        webhook=webhook,
        aibot=aibot,
    )
    return store, binance, detector, webhook, llm_compiler, aibot, monitor


async def _cmd_run(cfg: AppConfig) -> int:
    _, binance, _, webhook, llm_compiler, aibot, monitor = _build_components(cfg)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    if not webhook.is_configured and not cfg.wecom.aibot.is_ready:
        logger.warning(
            "Neither WeCom webhook_urls nor aibot credentials are configured; alerts will only be logged."
        )

    try:
        if cfg.wecom.aibot.is_ready:
            logger.info("Connecting WeCom Smart Bot long-connection...")
            await aibot.start()
        await monitor.run_forever(stop_event)
    finally:
        logger.info("Shutting down...")
        await aibot.stop()
        await llm_compiler.close()
        await webhook.close()
        await binance.close()
    return 0


async def _cmd_check_once(cfg: AppConfig, *, notify: bool) -> int:
    _, binance, _, webhook, llm_compiler, _, monitor = _build_components(cfg)
    try:
        summary = await monitor.poll_once(dry_run=not notify)
        print(
            f"Checked {summary.checked} series | signals={len(summary.signals)} "
            f"| rule_hits={len(summary.rule_hits)} | delivered={summary.delivered} "
            f"| deduped={summary.deduped} | errors={len(summary.errors)}"
        )
        for sig in summary.signals:
            print("-" * 50)
            print(format_signal_markdown(sig, cfg.alert.timezone))
        for hit in summary.rule_hits:
            print("-" * 50)
            print(
                format_custom_rule_alert_markdown(
                    rule_id=hit.rule.id,
                    rule_name=hit.rule.name,
                    rule_description=hit.rule.description,
                    symbol=hit.symbol,
                    display_name=hit.display_name,
                    interval=hit.rule.interval,
                    curr=hit.curr,
                    prev=hit.prev,
                    tz_name=cfg.alert.timezone,
                )
            )
        for err in summary.errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 0 if not summary.errors else 2
    finally:
        await llm_compiler.close()
        await webhook.close()
        await binance.close()


async def _cmd_test_webhook(cfg: AppConfig) -> int:
    webhook = WeComWebhookClient(cfg.wecom.webhook_urls)
    if not webhook.is_configured:
        print("ERROR: 未配置 WECOM_WEBHOOK_URL / wecom.webhook_urls", file=sys.stderr)
        return 1
    try:
        ok = await webhook.send_markdown(
            "### ✅ 币安 K 线形态监控 Bot 连通性测试\n"
            f"> 当前周期：`{cfg.binance.interval}`\n"
            f"> 初始标的数：`{len(cfg.binance.symbols)}`"
        )
        print("Webhook test message sent successfully." if ok else "Webhook test failed.")
        return 0 if ok else 1
    finally:
        await webhook.close()


async def _cmd_validate_symbols(cfg: AppConfig) -> int:
    store = StateStore(cfg.storage.sqlite_path)
    store.seed_symbols_if_empty(cfg.binance.symbols)
    targets = [s.symbol for s in store.list_symbols()] or cfg.binance.symbols

    binance = BinanceClient(
        base_url=cfg.binance.base_url,
        proxy=cfg.binance.proxy,
        timeout_seconds=cfg.binance.timeout_seconds,
    )
    failed = 0
    try:
        for sym in targets:
            try:
                resolved, display = await binance.resolve_symbol(sym)
                print(f"[OK] {sym} -> {resolved} ({display})")
            except SymbolNotFoundError as exc:
                failed += 1
                print(f"[FAIL] {exc}", file=sys.stderr)
    finally:
        await binance.close()
    return 0 if failed == 0 else 1


async def _cmd_test_rule(cfg: AppConfig, prompt: str, *, save: bool) -> int:
    store = StateStore(cfg.storage.sqlite_path)
    store.seed_symbols_if_empty(cfg.binance.symbols)
    watched = [s.symbol for s in store.list_symbols()]

    llm = DeepSeekRuleCompiler(cfg.llm)
    binance = BinanceClient(
        base_url=cfg.binance.base_url,
        proxy=cfg.binance.proxy,
        timeout_seconds=cfg.binance.timeout_seconds,
    )
    try:
        rule = await llm.compile_rule(
            prompt,
            default_interval=cfg.binance.interval,
            watched_symbols=watched,
            created_by="cli",
        )
        resolved: list[str] = []
        if rule.symbols == ["*"]:
            resolved = ["*"]
        else:
            for s in rule.symbols:
                sym, _ = await binance.resolve_symbol(s)
                if sym not in resolved:
                    resolved.append(sym)
        rule = CustomRule(
            id=None,
            name=rule.name,
            description=rule.description,
            symbols=resolved,
            interval=rule.interval,
            expression=rule.expression,
            lookback=rule.lookback,
            enabled=True,
            source_prompt=rule.source_prompt,
            created_by="cli",
        )

        print(f"Model      : {cfg.llm.model}")
        print(f"Name       : {rule.name}")
        print(f"Symbols    : {', '.join(rule.symbols)}")
        print(f"Interval   : {rule.interval} (lookback={rule.lookback})")
        print(f"Description: {rule.description}")
        print(f"Expression : {rule.expression}")
        print("-" * 50)

        samples = watched[:2] if rule.symbols == ["*"] else rule.symbols[:3]
        fetch_count = max(cfg.llm.backtest_candles, rule.lookback + 20)
        for sym in samples:
            closed = await binance.get_closed_klines(sym, rule.interval, count=fetch_count)
            hits = backtest_expression(rule.expression, closed, min_lookback=rule.lookback)
            latest = format_timestamp(hits[-1].open_time, cfg.alert.timezone) if hits else "无"
            print(f"[BACKTEST] {sym} ({rule.interval}, {len(closed)} candles): {len(hits)} hits | latest={latest}")

        if save:
            rule_id = store.add_rule(rule)
            print(f"Saved as rule #{rule_id} in {cfg.storage.sqlite_path}")
        return 0
    except (RuleCompileError, SymbolNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await llm.close()
        await binance.close()


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="binance-alert",
        description="Binance K-line engulfing & LLM custom pattern monitor for WeCom internal groups",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional .env file path",
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run continuous monitor + WeCom Smart Bot")

    check_p = sub.add_parser("check-once", help="Run a single poll check across all watched symbols & rules")
    check_p.add_argument(
        "--notify",
        action="store_true",
        help="Actually deliver alerts and update dedup state (default is dry-run)",
    )

    sub.add_parser("test-webhook", help="Send a test markdown message to configured WeCom webhook(s)")
    sub.add_parser("validate-symbols", help="Validate configured symbols against Binance exchangeInfo")

    rule_p = sub.add_parser("test-rule", help="Compile a natural-language rule via DeepSeek and backtest it")
    rule_p.add_argument("prompt", help="Natural-language pattern description")
    rule_p.add_argument("--save", action="store_true", help="Persist the compiled rule into SQLite")

    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        cfg = load_config(Path(args.config), env_path=args.env_file)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    _configure_logging(cfg.logging.level)

    if command == "run":
        rc = asyncio.run(_cmd_run(cfg))
    elif command == "check-once":
        rc = asyncio.run(_cmd_check_once(cfg, notify=bool(args.notify)))
    elif command == "test-webhook":
        rc = asyncio.run(_cmd_test_webhook(cfg))
    elif command == "validate-symbols":
        rc = asyncio.run(_cmd_validate_symbols(cfg))
    elif command == "test-rule":
        rc = asyncio.run(_cmd_test_rule(cfg, args.prompt, save=bool(args.save)))
    else:
        parser.print_help()
        rc = 1

    raise SystemExit(rc)


if __name__ == "__main__":
    cli()

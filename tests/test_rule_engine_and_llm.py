import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from binance_alert.binance_client import BinanceClient
from binance_alert.commands import CommandHandler
from binance_alert.config import AlertConfig, AppConfig, BinanceConfig, LLMConfig, WeComConfig
from binance_alert.detector import EngulfingDetector
from binance_alert.llm_compiler import DeepSeekRuleCompiler
from binance_alert.models import Kline
from binance_alert.monitor import KlineMonitor
from binance_alert.rule_engine import (
    UnsafeExpressionError,
    backtest_expression,
    evaluate_expression,
    validate_expression,
)
from binance_alert.store import StateStore
from binance_alert.webhook import WeComWebhookClient


def _make_kline(idx: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> Kline:
    open_time = 1_700_000_000_000 + idx * 3_600_000
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=open_time,
        close_time=open_time + 3_599_999,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def test_unsafe_expressions_rejected():
    bad_exprs = [
        "__import__('os').system('echo hi')",
        "curr.__class__.__mro__",
        "open('/etc/passwd')",
        "[x for x in range(10)]",
        "eval('1+1')",
        "curr.close > unknown_var",
    ]
    for expr in bad_exprs:
        with pytest.raises(UnsafeExpressionError):
            validate_expression(expr)


def test_rule_engine_patterns_and_indicators():
    # Build 20 steady candles then a hammer + breakout
    candles = [_make_kline(i, 100.0, 101.0, 99.0, 100.5, 100.0) for i in range(20)]
    # Add bearish candle then bullish engulfing with 2x volume
    candles.append(_make_kline(20, 101.0, 101.5, 99.5, 100.0, 100.0))
    candles.append(_make_kline(21, 99.8, 103.0, 99.5, 102.5, 220.0))

    assert evaluate_expression("bullish_engulfing(min_body_ratio=1.5, vol_mult=2.0)", candles) is True
    assert evaluate_expression("bullish_engulfing(min_body_ratio=1.5, vol_mult=3.0)", candles) is False
    assert evaluate_expression("curr.close > ma(20) and curr.volume > vol_ma(20, 1) * 2.0", candles) is True
    assert evaluate_expression("curr.close > highest_high(20, 1)", candles) is True

    # Add a long-lower-shadow hammer candle
    candles.append(_make_kline(22, 102.0, 102.2, 99.0, 102.5, 150.0))  # body=0.5, lower=3.0, upper=0.0? wait h=102.5
    candles[-1] = _make_kline(22, 102.0, 102.6, 99.0, 102.5, 150.0)    # body=0.5, lower=3.0, upper=0.1
    assert evaluate_expression("hammer(shadow_ratio=2.0)", candles) is True

    hits = backtest_expression("bullish_engulfing(vol_mult=2.0)", candles, min_lookback=2)
    assert len(hits) == 1
    assert hits[0].open_time == candles[21].open_time


@pytest.mark.asyncio
async def test_deepseek_compiler_with_self_correction():
    attempts = {"n": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        assert request.url.path == "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"

        if attempts["n"] == 1:
            # First attempt returns an invalid expression to test self-correction
            bad_json = json.dumps({
                "name": "放量阳包阴",
                "description": "测试",
                "symbols": ["NVDA"],
                "interval": "1h",
                "lookback": 5,
                "expression": "bad_func() == True",
            })
            return httpx.Response(200, json={"choices": [{"message": {"content": bad_json}}]})

        good_json = json.dumps({
            "name": "放量1.5倍阳包阴",
            "description": "1小时已收盘出现阳包阴且放量1.5倍",
            "symbols": ["NVDA"],
            "interval": "1h",
            "lookback": 5,
            "expression": "bullish_engulfing(min_body_ratio=1.0, vol_mult=1.5)",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": good_json}}]})

    compiler = DeepSeekRuleCompiler(
        LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash"),
        transport=httpx.MockTransport(llm_handler),
    )
    rule = await compiler.compile_rule("NVDA 1小时线放量1.5倍阳包阴")
    await compiler.close()

    assert attempts["n"] == 2
    assert rule.name == "放量1.5倍阳包阴"
    assert rule.symbols == ["NVDA"]
    assert rule.interval == "1h"
    assert "bullish_engulfing" in rule.expression


@pytest.mark.asyncio
async def test_nl_rule_command_confirm_and_monitor_dispatch(tmp_path: Path):
    exchange_info = {"symbols": [{"symbol": "NVDABUSDT", "status": "TRADING"}]}
    # 5 closed candles + 1 forming candle; candle #4 is bullish engulfing with 2x volume
    raw_klines = [
        [1_700_000_000_000, "160.0", "161.0", "159.0", "160.5", "100", 1_700_003_599_999],
        [1_700_003_600_000, "160.5", "162.0", "160.0", "161.5", "100", 1_700_007_199_999],
        [1_700_007_200_000, "162.5", "163.0", "161.0", "161.2", "100", 1_700_010_799_999],
        [1_700_010_800_000, "161.0", "164.0", "160.8", "163.8", "200", 1_700_014_399_999],
        [1_700_014_400_000, "163.8", "164.2", "163.5", "164.0", "50", 1_999_999_999_999],
    ]

    def binance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=exchange_info)
        return httpx.Response(200, json=raw_klines)

    def llm_handler(request: httpx.Request) -> httpx.Response:
        good_json = json.dumps({
            "name": "放量阳包阴",
            "description": "1h放量1.5倍阳包阴",
            "symbols": ["NVDA"],
            "interval": "1h",
            "lookback": 4,
            "expression": "bullish_engulfing(min_body_ratio=1.0, vol_mult=1.5)",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": good_json}}]})

    sent_msgs: list[str] = []

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent_msgs.append(payload["markdown"]["content"])
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    cfg = AppConfig(
        binance=BinanceConfig(symbols=["NVDABUSDT"], interval="1h"),
        wecom=WeComConfig(webhook_urls=["https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test12345678"]),
        llm=LLMConfig(api_key="sk-test", model="deepseek-flash"),
        alert=AlertConfig(enable_default_engulfing=False),
    )
    store = StateStore(tmp_path / "nl.sqlite3")
    store.seed_symbols_if_empty(cfg.binance.symbols)

    async with BinanceClient(transport=httpx.MockTransport(binance_handler)) as binance:
        compiler = DeepSeekRuleCompiler(cfg.llm, transport=httpx.MockTransport(llm_handler))
        webhook = WeComWebhookClient(cfg.wecom.webhook_urls, transport=httpx.MockTransport(webhook_handler))
        cmd = CommandHandler(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
            llm_compiler=compiler,
        )

        # Step 1: Natural-language rule preview + backtest
        preview = await cmd.handle("@监控助手 新增规则 NVDA 1小时线放量1.5倍阳包阴", user_id="u1")
        assert "NVDABUSDT" in preview
        assert "历史回测预览" in preview
        assert "触发 **1 次**" in preview
        assert store.list_rules() == []

        # Step 2: Confirm saves rule
        confirmed = await cmd.handle("@监控助手 确认", user_id="u1")
        assert "已激活" in confirmed
        assert len(store.list_rules(enabled_only=True)) == 1

        # Step 3: Monitor evaluates custom rule, delivers once, deduplicates second poll
        monitor = KlineMonitor(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
            webhook=webhook,
        )
        s1 = await monitor.poll_once()
        assert len(s1.rule_hits) == 1
        assert s1.delivered == 1
        assert "放量阳包阴" in sent_msgs[0]

        s2 = await monitor.poll_once()
        assert s2.delivered == 0
        assert s2.deduped == 1

        # Step 4: History formats custom rule cleanly
        hist = await cmd.handle("历史", user_id="u1")
        assert "放量阳包阴" in hist

        await compiler.close()
        await webhook.close()


def test_aibot_bridge_builds_real_ws_client(tmp_path: Path):
    from binance_alert.aibot_client import WeComAiBotBridge
    from binance_alert.config import AiBotConfig

    cfg = AppConfig()
    store = StateStore(tmp_path / "ws.sqlite3")
    binance = BinanceClient()
    cmd = CommandHandler(config=cfg, store=store, binance=binance, detector=EngulfingDetector())
    bridge = WeComAiBotBridge(
        config=AiBotConfig(enabled=True, bot_id="bot123", secret="sec123"),
        store=store,
        command_handler=cmd,
    )
    ws_client = bridge._build_client()
    assert ws_client is not None


@pytest.mark.asyncio
async def test_natural_language_multi_intent_routing(tmp_path: Path):
    exchange_info = {
        "symbols": [
            {"symbol": "AAPLBUSDT", "status": "TRADING"},
            {"symbol": "NVDABUSDT", "status": "TRADING"},
            {"symbol": "MSFTBUSDT", "status": "TRADING"},
        ]
    }
    raw_klines = [
        [1_700_000_000_000, "162.5", "163.0", "161.0", "161.2", "100", 1_700_003_599_999],
        [1_700_003_600_000, "161.0", "164.0", "160.8", "163.8", "200", 1_700_007_199_999],
        [1_700_007_200_000, "163.8", "164.2", "163.5", "164.0", "50", 1_999_999_999_999],
    ]

    def binance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=exchange_info)
        return httpx.Response(200, json=raw_klines)

    def llm_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "把苹果删了" in user_msg:
            resp = {
                "actions": [
                    {"action": "remove_symbols", "symbols": ["AAPLBUSDT"]},
                    {"action": "add_symbols", "symbols": ["MSFT"]},
                    {"action": "check", "symbols": ["NVDA"]},
                ]
            }
        elif "好的，开启吧" in user_msg:
            resp = {"actions": [{"action": "confirm_rule"}]}
        elif "关掉" in user_msg:
            resp = {"actions": [{"action": "set_rule_enabled", "rule_id": 1, "enabled": False}]}
        else:
            resp = {
                "name": "放量阳包阴",
                "description": "1h放量阳包阴",
                "symbols": ["NVDA"],
                "interval": "1h",
                "lookback": 3,
                "expression": "bullish_engulfing(vol_mult=1.5)",
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(resp)}}]})

    from binance_alert.config import AiBotConfig

    cfg = AppConfig(
        binance=BinanceConfig(symbols=["AAPLBUSDT", "NVDABUSDT"], interval="1h"),
        wecom=WeComConfig(aibot=AiBotConfig(allowed_users=["admin1"])),
        llm=LLMConfig(api_key="sk-test", model="deepseek-flash"),
    )
    store = StateStore(tmp_path / "router.sqlite3")
    store.seed_symbols_if_empty(cfg.binance.symbols)

    async with BinanceClient(transport=httpx.MockTransport(binance_handler)) as binance:
        compiler = DeepSeekRuleCompiler(cfg.llm, transport=httpx.MockTransport(llm_handler))
        cmd = CommandHandler(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
            llm_compiler=compiler,
        )

        # Guest cannot mutate via natural language, but read-only check still executes
        guest_reply = await cmd.handle("@机器人 把苹果删了，加上微软，再看看英伟达", user_id="guest")
        assert "没有权限" in guest_reply
        assert "阳包阴" in guest_reply
        assert "AAPLBUSDT" in [s.symbol for s in store.list_symbols()]

        # Admin executes multi-intent cleanly
        admin_reply = await cmd.handle("@机器人 把苹果删了，加上微软，再看看英伟达", user_id="admin1")
        assert "已移除 `AAPLBUSDT`" in admin_reply
        assert "MSFTBUSDT" in admin_reply
        assert "阳包阴" in admin_reply
        assert set(s.symbol for s in store.list_symbols()) == {"NVDABUSDT", "MSFTBUSDT"}

        # Natural-language confirm + disable rule
        await cmd.handle("新增规则 NVDA 放量阳包阴", user_id="admin1")
        confirm_reply = await cmd.handle("@机器人 好的，开启吧", user_id="admin1")
        assert "已激活" in confirm_reply
        assert store.list_rules(enabled_only=True)[0].id == 1

        disable_reply = await cmd.handle("@机器人 把1号规则先关掉", user_id="admin1")
        assert "已停用" in disable_reply
        assert store.list_rules(enabled_only=True) == []

        await compiler.close()

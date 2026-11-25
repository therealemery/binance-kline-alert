from pathlib import Path

import httpx
import pytest

from binance_alert.binance_client import BinanceClient
from binance_alert.commands import CommandHandler, strip_mentions
from binance_alert.config import AiBotConfig, AppConfig, BinanceConfig, WeComConfig
from binance_alert.detector import EngulfingDetector
from binance_alert.monitor import KlineMonitor
from binance_alert.store import StateStore
from binance_alert.webhook import WeComWebhookClient


def test_strip_mentions():
    assert strip_mentions("@行情Bot\u2005添加 NVDA") == "添加 NVDA"
    assert strip_mentions("@Bot1 @Bot2   status") == "status"
    assert strip_mentions("状态") == "状态"


@pytest.mark.asyncio
async def test_commands_and_authorization(tmp_path: Path):
    exchange_info = {"symbols": [{"symbol": "NVDABUSDT", "status": "TRADING"}]}
    klines = [
        [1_700_000_000_000, "162.5", "163.0", "161.0", "161.2", "1000", 1_700_003_599_999],
        [1_700_003_600_000, "161.1", "164.0", "160.9", "163.8", "1500", 1_700_007_199_999],
        [1_700_007_200_000, "163.8", "164.5", "163.5", "164.1", "200", 1_999_999_999_999],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=exchange_info)
        return httpx.Response(200, json=klines)

    cfg = AppConfig(
        binance=BinanceConfig(symbols=[]),
        wecom=WeComConfig(aibot=AiBotConfig(allowed_users=["admin1"])),
    )
    store = StateStore(tmp_path / "cmd.sqlite3")
    async with BinanceClient(transport=httpx.MockTransport(handler)) as binance:
        cmd = CommandHandler(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
        )

        # Unauthorized user blocked on mutating commands
        denied = await cmd.handle("@监控助手 添加 NVDA", user_id="guest")
        assert "没有权限" in denied

        # Authorized user can add, check, pause, resume, remove
        added = await cmd.handle("@监控助手 添加 NVDA", user_id="admin1")
        assert "NVDABUSDT" in added
        assert [s.symbol for s in store.list_symbols()] == ["NVDABUSDT"]

        checked = await cmd.handle("检查 NVDA", user_id="guest")
        assert "阳包阴" in checked

        paused = await cmd.handle("暂停", user_id="admin1")
        assert "已暂停" in paused
        assert store.is_paused() is True

        resumed = await cmd.handle("恢复", user_id="admin1")
        assert "已恢复" in resumed
        assert store.is_paused() is False

        removed = await cmd.handle("删除 NVDA", user_id="admin1")
        assert "NVDABUSDT" in removed
        assert store.list_symbols() == []


@pytest.mark.asyncio
async def test_monitor_poll_dedup_and_retry_on_webhook_failure(tmp_path: Path):
    klines = [
        [1_700_000_000_000, "162.5", "163.0", "161.0", "161.2", "1000", 1_700_003_599_999],
        [1_700_003_600_000, "161.1", "164.0", "160.9", "163.8", "1500", 1_700_007_199_999],
        [1_700_007_200_000, "163.8", "164.5", "163.5", "164.1", "200", 1_999_999_999_999],
    ]
    webhook_calls: list[dict] = []
    should_fail = {"value": True}

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=klines)

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        import json

        webhook_calls.append(json.loads(request.content))
        if should_fail["value"]:
            return httpx.Response(200, json={"errcode": 93000, "errmsg": "temporary error"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    cfg = AppConfig(
        binance=BinanceConfig(symbols=["NVDABUSDT"], interval="1h"),
        wecom=WeComConfig(webhook_urls=["https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test12345678"]),
    )
    store = StateStore(tmp_path / "monitor.sqlite3")
    store.seed_symbols_if_empty(cfg.binance.symbols)

    async with BinanceClient(transport=httpx.MockTransport(binance_handler)) as binance:
        webhook = WeComWebhookClient(
            cfg.wecom.webhook_urls,
            transport=httpx.MockTransport(webhook_handler),
        )
        monitor = KlineMonitor(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
            webhook=webhook,
        )

        # First poll fails webhook -> should NOT record dedup state
        s1 = await monitor.poll_once()
        assert len(s1.signals) == 1
        assert s1.delivered == 0
        assert store.get_last_alert_open_time("NVDABUSDT", "1h") is None

        # Second poll succeeds -> delivers once and records dedup state
        should_fail["value"] = False
        s2 = await monitor.poll_once()
        assert s2.delivered == 1
        assert store.get_last_alert_open_time("NVDABUSDT", "1h") == 1_700_003_600_000
        assert "阳包阴" in webhook_calls[-1]["markdown"]["content"]

        # Third poll on same candle -> deduplicated
        s3 = await monitor.poll_once()
        assert s3.delivered == 0
        assert s3.deduped == 1

        await webhook.close()

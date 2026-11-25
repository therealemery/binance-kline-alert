import json
from pathlib import Path

import httpx
import pytest

from binance_alert.binance_client import BinanceClient
from binance_alert.commands import CommandHandler, strip_mentions
from binance_alert.config import AiBotConfig, AppConfig, BinanceConfig, TelegramConfig, WeComConfig
from binance_alert.detector import EngulfingDetector
from binance_alert.monitor import KlineMonitor
from binance_alert.store import StateStore
from binance_alert.telegram_client import (
    TelegramBotClient,
    markdown_to_telegram_html,
    mask_telegram_token,
    split_telegram_message,
)
from binance_alert.webhook import WeComWebhookClient


def test_markdown_to_telegram_html_and_splitting():
    md = (
        '### 📈 <font color="info">阳包阴信号</font>\n\n'
        "> **标的**：NVDAB & NVDA (`NVDABUSDT`)\n"
        "> **涨幅**：`+1.65%` (<2%)\n\n"
        "[查看币安图表](https://www.binance.com/zh-CN/trade/NVDAB_USDT)"
    )
    rendered = markdown_to_telegram_html(md)

    assert "<font" not in rendered
    assert "###" not in rendered
    assert "<b>📈 阳包阴信号</b>" in rendered
    assert "• <b>标的</b>：NVDAB &amp; NVDA (<code>NVDABUSDT</code>)" in rendered
    assert "(&lt;2%)" in rendered
    assert '<a href="https://www.binance.com/zh-CN/trade/NVDAB_USDT">查看币安图表</a>' in rendered

    long_text = "\n".join(f"line {i}: " + ("x" * 100) for i in range(60))
    chunks = split_telegram_message(long_text, max_len=1000)
    assert len(chunks) >= 6
    assert all(len(c) <= 1000 for c in chunks)


def test_strip_mentions_and_slash_commands():
    assert strip_mentions("/status") == "status"
    assert strip_mentions("/check@my_alert_bot BTC") == "check BTC"
    assert strip_mentions("/start@my_alert_bot") == "start"
    assert strip_mentions("@my_alert_bot 看看 BTC") == "看看 BTC"
    assert strip_mentions("@行情Bot\u2005添加 NVDA") == "添加 NVDA"


def test_mask_telegram_token():
    raw = "Client error for url 'https://api.telegram.org/bot123456:ABC-def_999/getUpdates'"
    masked = mask_telegram_token(raw, "123456:ABC-def_999")
    assert "123456:ABC-def_999" not in masked
    assert "bot***" in masked


@pytest.mark.asyncio
async def test_telegram_send_markdown_fallback_and_retry(tmp_path: Path):
    calls: list[dict] = []
    mode = {"scenario": "ok"}

    def tg_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        payload = json.loads(request.content)
        calls.append(payload)

        if mode["scenario"] == "html_400":
            if payload.get("parse_mode") == "HTML":
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

        if mode["scenario"] == "rate_429":
            if len(calls) == 1:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0.01}})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    store = StateStore(tmp_path / "tg.sqlite3")
    tg = TelegramBotClient(
        config=TelegramConfig(bot_token="12345:TESTTOKEN", chat_ids=["-100123"]),
        store=store,
        transport=httpx.MockTransport(tg_handler),
    )

    # 1. Happy path
    assert await tg.send_markdown("### Hello **World** `BTCUSDT`") is True
    assert calls[-1]["parse_mode"] == "HTML"
    assert "<b>Hello <b>World</b> <code>BTCUSDT</code></b>" in calls[-1]["text"]

    # 2. HTTP 400 HTML error -> automatic plain-text fallback
    calls.clear()
    mode["scenario"] = "html_400"
    assert await tg.send_markdown("### Alert <font color=\"info\">Test</font>") is True
    assert len(calls) == 2
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]
    assert "<font" not in calls[1]["text"]

    # 3. HTTP 429 rate limit -> retry using retry_after
    calls.clear()
    mode["scenario"] = "rate_429"
    assert await tg.send_markdown("Rate limit test") is True
    assert len(calls) == 2

    await tg.stop()


@pytest.mark.asyncio
async def test_telegram_update_handling_and_channel_auth(tmp_path: Path):
    replies: list[dict] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "kline_demo_bot"}})
        if request.url.path.endswith("/sendMessage"):
            replies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        return httpx.Response(200, json={"ok": True, "result": []})

    cfg = AppConfig(
        binance=BinanceConfig(symbols=["BTCUSDT"]),
        wecom=WeComConfig(aibot=AiBotConfig(allowed_users=["wecom_admin"])),
        telegram=TelegramConfig(
            bot_token="12345:TESTTOKEN",
            enable_polling=False,
            allowed_users=["10001"],
            require_mention_in_group=True,
        ),
    )
    store = StateStore(tmp_path / "tg_cmd.sqlite3")
    store.seed_symbols_if_empty(cfg.binance.symbols)
    binance = BinanceClient()
    cmd = CommandHandler(config=cfg, store=store, binance=binance, detector=EngulfingDetector())

    tg = TelegramBotClient(
        config=cfg.telegram,
        store=store,
        command_handler=cmd,
        transport=httpx.MockTransport(tg_handler),
    )
    await tg.start()
    assert tg.bot_username == "kline_demo_bot"

    # 1. Group chatter without @mention or /command should be ignored
    await tg._handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 10001, "is_bot": False},
                "chat": {"id": -100555, "type": "supergroup"},
                "text": "hello everyone",
            },
        }
    )
    assert replies == []

    # 2. Group /status@kline_demo_bot works and registers chat_id
    await tg._handle_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "from": {"id": 99999, "is_bot": False},
                "chat": {"id": -100555, "type": "supergroup"},
                "text": "/status@kline_demo_bot",
            },
        }
    )
    assert len(replies) == 1
    assert replies[-1]["chat_id"] == "-100555"
    assert replies[-1]["reply_to_message_id"] == 11
    assert "监控运行状态" in replies[-1]["text"]
    assert "-100555" in store.get_telegram_chat_ids()
    assert store.get_chat_ids() == []  # Isolated from WeCom chat_ids

    # 3. Channel-isolated authorization: unauthorized Telegram user blocked on /pause
    await tg._handle_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "from": {"id": 99999, "is_bot": False},
                "chat": {"id": -100555, "type": "supergroup"},
                "text": "/pause",
            },
        }
    )
    assert "没有权限" in replies[-1]["text"]
    assert store.is_paused() is False

    # 4. Reply directly to bot message in group (without @mention) is accepted
    await tg._handle_update(
        {
            "update_id": 4,
            "message": {
                "message_id": 13,
                "from": {"id": 10001, "is_bot": False},
                "chat": {"id": -100555, "type": "supergroup"},
                "reply_to_message": {"from": {"id": 12345, "is_bot": True, "username": "kline_demo_bot"}},
                "text": "pause",
            },
        }
    )
    assert "已暂停" in replies[-1]["text"]
    assert store.is_paused() is True

    await tg.stop()
    await binance.close()


@pytest.mark.asyncio
async def test_monitor_pure_telegram_mode_delivers_and_dedups(tmp_path: Path):
    klines = [
        [1_700_000_000_000, "162.5", "163.0", "161.0", "161.2", "1000", 1_700_003_599_999],
        [1_700_003_600_000, "161.1", "164.0", "160.9", "163.8", "1500", 1_700_007_199_999],
        [1_700_007_200_000, "163.8", "164.5", "163.5", "164.1", "200", 1_999_999_999_999],
    ]
    tg_sent: list[dict] = []

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=klines)

    def tg_handler(request: httpx.Request) -> httpx.Response:
        tg_sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    cfg = AppConfig(
        binance=BinanceConfig(symbols=["NVDABUSDT"], interval="1h"),
        wecom=WeComConfig(webhook_urls=[]),
        telegram=TelegramConfig(bot_token="12345:TESTTOKEN", chat_ids=["-100999"], push_alerts=True),
    )
    store = StateStore(tmp_path / "pure_tg.sqlite3")
    store.seed_symbols_if_empty(cfg.binance.symbols)

    async with BinanceClient(transport=httpx.MockTransport(binance_handler)) as binance:
        webhook = WeComWebhookClient([])
        telegram = TelegramBotClient(
            config=cfg.telegram,
            store=store,
            transport=httpx.MockTransport(tg_handler),
        )
        monitor = KlineMonitor(
            config=cfg,
            store=store,
            binance=binance,
            detector=EngulfingDetector(),
            webhook=webhook,
            telegram=telegram,
        )

        s1 = await monitor.poll_once()
        assert len(s1.signals) == 1
        assert s1.delivered == 1
        assert len(tg_sent) == 1
        assert tg_sent[0]["chat_id"] == "-100999"
        assert "阳包阴" in tg_sent[0]["text"]
        assert store.get_last_alert_open_time("NVDABUSDT", "1h") == 1_700_003_600_000

        # Second poll on same candle deduplicated
        s2 = await monitor.poll_once()
        assert s2.delivered == 0
        assert s2.deduped == 1

        await telegram.stop()
        await webhook.close()

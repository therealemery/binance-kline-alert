from decimal import Decimal

import httpx
import pytest

from binance_alert.binance_client import BinanceClient, SymbolNotFoundError


@pytest.mark.asyncio
async def test_get_last_two_closed_klines_ignores_forming_candle():
    now_ms = 1_700_007_250_000  # 50s into the 3rd hour
    raw_klines = [
        # Closed candle 1 (prev)
        [1_700_000_000_000, "162.5", "163.0", "161.0", "161.2", "1000", 1_700_003_599_999],
        # Closed candle 2 (curr)
        [1_700_003_600_000, "161.1", "164.0", "160.9", "163.8", "1500", 1_700_007_199_999],
        # Currently forming candle 3 (must be ignored!)
        [1_700_007_200_000, "163.8", "164.5", "163.5", "164.1", "200", 1_700_010_799_999],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/klines"
        assert request.url.params["limit"] == "3"
        return httpx.Response(200, json=raw_klines)

    async with BinanceClient(transport=httpx.MockTransport(handler)) as client:
        prev, curr = await client.get_last_two_closed_klines("NVDABUSDT", "1h", now_ms=now_ms)

    assert prev.open_time == 1_700_000_000_000
    assert curr.open_time == 1_700_003_600_000
    assert curr.close == Decimal("163.8")


@pytest.mark.asyncio
async def test_resolve_symbol_supports_crypto_and_bstocks():
    exchange_info = {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING"},
            {"symbol": "NVDABUSDT", "status": "TRADING"},
            {"symbol": "OLDUSDT", "status": "BREAK"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/exchangeInfo"
        return httpx.Response(200, json=exchange_info)

    async with BinanceClient(transport=httpx.MockTransport(handler)) as client:
        sym1, name1 = await client.resolve_symbol("btc")
        assert sym1 == "BTCUSDT"
        assert name1 == "BTC/USDT"

        sym2, name2 = await client.resolve_symbol("NVDA")
        assert sym2 == "NVDABUSDT"
        assert "NVIDIA" in name2

        sym3, _ = await client.resolve_symbol("NVDAB/USDT")
        assert sym3 == "NVDABUSDT"

        with pytest.raises(SymbolNotFoundError):
            await client.resolve_symbol("OLD")

        with pytest.raises(SymbolNotFoundError):
            await client.resolve_symbol("NONEXISTENT")

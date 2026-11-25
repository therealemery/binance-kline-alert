from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import Decimal
from typing import Any

import httpx

from .formatter import infer_display_name
from .models import Kline

logger = logging.getLogger(__name__)


class SymbolNotFoundError(ValueError):
    """Raised when a user-provided symbol cannot be resolved on Binance Spot."""


class BinanceClient:
    """Async Binance Spot REST client with proxy support, retries, and symbol validation."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.binance.com",
        proxy: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.timeout_seconds = timeout_seconds
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": httpx.Timeout(timeout_seconds),
            "headers": {"User-Agent": "binance-kline-alert/0.1.0"},
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._symbols_cache: set[str] = set()
        self._symbols_cached_at: float = 0.0
        self._cache_lock = asyncio.Lock()

    async def __aenter__(self) -> BinanceClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", attempt * 2))
                    logger.warning("Binance rate limited (429), retrying in %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Binance server error {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                if attempt < max_retries:
                    delay = min(1.5 * attempt, 5.0)
                    logger.warning("Binance request %s failed (attempt %d/%d): %s", path, attempt, max_retries, exc)
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def fetch_trading_symbols(self, *, force_refresh: bool = False) -> set[str]:
        """Fetch active TRADING symbols from /api/v3/exchangeInfo with 1-hour cache."""
        now = time.monotonic()
        if not force_refresh and self._symbols_cache and (now - self._symbols_cached_at) < 3600:
            return self._symbols_cache

        async with self._cache_lock:
            now = time.monotonic()
            if not force_refresh and self._symbols_cache and (now - self._symbols_cached_at) < 3600:
                return self._symbols_cache
            payload = await self._request_json("/api/v3/exchangeInfo", params={"symbolStatus": "TRADING"})
            symbols = {
                str(item["symbol"]).upper()
                for item in payload.get("symbols", [])
                if item.get("status") == "TRADING"
            }
            self._symbols_cache = symbols
            self._symbols_cached_at = time.monotonic()
            return self._symbols_cache

    @staticmethod
    def _candidate_symbols(raw_input: str) -> list[str]:
        norm = re.sub(r"[^A-Za-z0-9]", "", raw_input).upper()
        if not norm:
            return []
        candidates = [norm]
        if not norm.endswith("USDT"):
            candidates.append(f"{norm}USDT")
            if not norm.endswith("B"):
                candidates.append(f"{norm}BUSDT")
        return list(dict.fromkeys(candidates))

    async def resolve_symbol(self, raw_input: str) -> tuple[str, str]:
        """Resolve user input (e.g. 'BTC', 'NVDA', 'NVDABUSDT') to a verified Binance Spot symbol."""
        candidates = self._candidate_symbols(raw_input)
        if not candidates:
            raise SymbolNotFoundError("标的不能为空")

        trading_symbols = await self.fetch_trading_symbols()
        for cand in candidates:
            if cand in trading_symbols:
                return cand, infer_display_name(cand)

        # Retry once with fresh exchangeInfo if cache is older than 60s
        if (time.monotonic() - self._symbols_cached_at) > 60:
            trading_symbols = await self.fetch_trading_symbols(force_refresh=True)
            for cand in candidates:
                if cand in trading_symbols:
                    return cand, infer_display_name(cand)

        tried = ", ".join(candidates)
        raise SymbolNotFoundError(f"未在币安现货找到可交易标的 `{raw_input}`（已尝试：{tried}）")

    async def get_closed_klines(
        self,
        symbol: str,
        interval: str = "1h",
        *,
        count: int = 2,
        now_ms: int | None = None,
    ) -> list[Kline]:
        """Fetch latest `count + 1` candles and return up to the latest `count` CLOSED candles."""
        limit = min(max(count + 1, 3), 1000)
        raw_klines = await self._request_json(
            "/api/v3/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff_ms = current_ms - 1000

        closed: list[Kline] = []
        for row in raw_klines:
            close_time = int(row[6])
            if close_time <= cutoff_ms:
                closed.append(
                    Kline(
                        symbol=symbol.upper(),
                        interval=interval,
                        open_time=int(row[0]),
                        close_time=close_time,
                        open=Decimal(str(row[1])),
                        high=Decimal(str(row[2])),
                        low=Decimal(str(row[3])),
                        close=Decimal(str(row[4])),
                        volume=Decimal(str(row[5])),
                    )
                )
        return closed[-count:] if len(closed) > count else closed

    async def get_last_two_closed_klines(
        self,
        symbol: str,
        interval: str = "1h",
        *,
        now_ms: int | None = None,
    ) -> tuple[Kline, Kline]:
        """Fetch latest 3 candles and return the two most recent CLOSED candles (prev, curr)."""
        closed = await self.get_closed_klines(symbol, interval, count=2, now_ms=now_ms)
        if len(closed) < 2:
            raise ValueError(
                f"Expected at least 2 closed klines for {symbol} ({interval}), got {len(closed)}"
            )
        return closed[-2], closed[-1]

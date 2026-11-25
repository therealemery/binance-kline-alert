from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_KEY_MASK_RE = re.compile(r"(key=)([^&]{4})[^&]*([^&]{4})")


def _mask_url(url: str) -> str:
    return _KEY_MASK_RE.sub(r"\1\2****\3", url)


class WeComWebhookClient:
    """WeCom group robot Webhook sender with sliding-window rate limiting and retries."""

    def __init__(
        self,
        webhook_urls: list[str],
        *,
        timeout_seconds: float = 10.0,
        max_per_minute: int = 18,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.webhook_urls = [u.strip() for u in webhook_urls if u.strip()]
        self.max_per_minute = max(1, min(max_per_minute, 20))
        client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(timeout_seconds)}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._send_times: dict[str, deque[float]] = {url: deque() for url in self.webhook_urls}
        self._locks: dict[str, asyncio.Lock] = {url: asyncio.Lock() for url in self.webhook_urls}

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_urls)

    async def close(self) -> None:
        await self._client.aclose()

    async def _wait_rate_limit(self, url: str) -> None:
        window = self._send_times[url]
        now = time.monotonic()
        while window and (now - window[0]) >= 60.0:
            window.popleft()
        if len(window) >= self.max_per_minute:
            sleep_for = 60.5 - (now - window[0])
            if sleep_for > 0:
                logger.info("Webhook rate-limit pacing: sleeping %.1fs", sleep_for)
                await asyncio.sleep(sleep_for)
            now = time.monotonic()
            while window and (now - window[0]) >= 60.0:
                window.popleft()

    async def _post_one(self, url: str, content: str, *, max_retries: int = 3) -> bool:
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        masked = _mask_url(url)

        async with self._locks[url]:
            for attempt in range(1, max_retries + 1):
                await self._wait_rate_limit(url)
                try:
                    resp = await self._client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    self._send_times[url].append(time.monotonic())
                    errcode = data.get("errcode", -1)
                    if errcode == 0:
                        return True
                    logger.error(
                        "WeCom webhook error (%s): errcode=%s errmsg=%s",
                        masked,
                        errcode,
                        data.get("errmsg"),
                    )
                    # 45009 = frequency limit; wait and retry
                    if errcode == 45009 and attempt < max_retries:
                        await asyncio.sleep(5.0 * attempt)
                        continue
                    return False
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                    logger.warning(
                        "WeCom webhook request failed (%s, attempt %d/%d): %s",
                        masked,
                        attempt,
                        max_retries,
                        exc,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(1.5 * attempt)
        return False

    async def send_markdown(self, content: str) -> bool:
        if not self.webhook_urls:
            return False
        results = await asyncio.gather(*(self._post_one(url, content) for url in self.webhook_urls))
        return any(results)

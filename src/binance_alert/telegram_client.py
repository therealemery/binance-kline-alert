from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

from .config import TelegramConfig
from .store import StateStore

if TYPE_CHECKING:
    from .commands import CommandHandler

logger = logging.getLogger(__name__)

_WECOM_FONT_RE = re.compile(r'<font\s+color="[^"]+">(.*?)</font>', re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_QUOTE_RE = re.compile(r"^&gt;\s?", re.MULTILINE)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_TOKEN_MASK_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def mask_telegram_token(text: str, token: str = "") -> str:
    masked = _TOKEN_MASK_RE.sub("bot***", text)
    if token:
        masked = masked.replace(token, "***")
    return masked


def markdown_to_telegram_html(md: str) -> str:
    """Convert WeCom-flavored Markdown into Telegram-safe HTML (parse_mode='HTML')."""
    text = _WECOM_FONT_RE.sub(r"\1", md)
    text = html.escape(text, quote=False)
    text = _HEADING_RE.sub(r"<b>\1</b>", text)
    text = _QUOTE_RE.sub("• ", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    return text


def split_telegram_message(text: str, *, max_len: int = 4096) -> list[str]:
    """Split a long message on newline boundaries to stay within Telegram's 4096-char limit."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if current_lines:
                chunks.append("".join(current_lines).rstrip("\n"))
                current_lines = []
                current_len = 0
            for idx in range(0, len(line), max_len):
                chunks.append(line[idx : idx + max_len])
            continue

        if current_len + len(line) > max_len and current_lines:
            chunks.append("".join(current_lines).rstrip("\n"))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += len(line)

    if current_lines:
        chunks.append("".join(current_lines).rstrip("\n"))
    return [c for c in chunks if c]


class TelegramBotClient:
    """Zero-extra-dependency Telegram Bot client using httpx for alerts and getUpdates polling."""

    def __init__(
        self,
        *,
        config: TelegramConfig,
        store: StateStore,
        command_handler: CommandHandler | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.command_handler = command_handler
        self._bot_username: str | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._offset: int = 0
        self._running: bool = False

        base_url = f"https://api.telegram.org/bot{config.bot_token}"
        client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": httpx.Timeout(config.timeout_seconds),
            "headers": {"User-Agent": "binance-kline-alert/0.1.0"},
        }
        if config.proxy:
            client_kwargs["proxy"] = config.proxy
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    def _target_chat_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.config.chat_ids, *self.store.get_telegram_chat_ids()]))

    @property
    def is_configured(self) -> bool:
        return self.config.is_ready and self.config.push_alerts and bool(self._target_chat_ids())

    @property
    def is_connected(self) -> bool:
        return self._running and self._polling_task is not None and not self._polling_task.done()

    @property
    def bot_username(self) -> str | None:
        return self._bot_username

    async def start(self) -> None:
        if not self.config.is_ready:
            logger.info("Telegram Bot not configured; skipping startup")
            return

        try:
            resp = await self._client.get("/getMe")
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                self._bot_username = str((data.get("result") or {}).get("username") or "").strip() or None
                logger.info("Telegram Bot authenticated as @%s", self._bot_username or "unknown")
            else:
                logger.error("Telegram getMe failed: %s", data.get("description"))
        except Exception as exc:
            logger.error("Telegram getMe error: %s", mask_telegram_token(str(exc), self.config.bot_token))

        if self.config.enable_polling and self.command_handler is not None:
            self._running = True
            self._polling_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
        await self._client.aclose()

    async def _post_chunk(
        self,
        chat_id: str,
        html_text: str,
        plain_fallback: str,
        *,
        reply_to_message_id: int | None = None,
        max_retries: int = 3,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        for attempt in range(1, max_retries + 1):
            try:
                resp = await self._client.post("/sendMessage", json=payload)
                data: dict[str, Any] = {}
                try:
                    data = resp.json()
                except Exception:
                    pass

                if resp.status_code == 200 and data.get("ok"):
                    return True

                if resp.status_code == 429:
                    retry_after = float((data.get("parameters") or {}).get("retry_after", attempt * 2))
                    logger.warning("Telegram rate limited (429), retrying in %.1fs", retry_after)
                    if attempt < max_retries:
                        await asyncio.sleep(retry_after)
                        continue
                    return False

                if resp.status_code == 400 and payload.get("parse_mode") == "HTML":
                    logger.warning(
                        "Telegram HTML parse rejected (400: %s); retrying as plain text",
                        data.get("description", ""),
                    )
                    fallback_payload: dict[str, Any] = {
                        "chat_id": chat_id,
                        "text": plain_fallback[:4096],
                        "disable_web_page_preview": True,
                    }
                    if reply_to_message_id is not None:
                        fallback_payload["reply_to_message_id"] = reply_to_message_id
                    fb_resp = await self._client.post("/sendMessage", json=fallback_payload)
                    fb_data = fb_resp.json() if fb_resp.headers.get("content-type", "").startswith("application/json") else {}
                    return fb_resp.status_code == 200 and bool(fb_data.get("ok"))

                logger.error(
                    "Telegram sendMessage failed (chat=%s status=%s): %s",
                    chat_id,
                    resp.status_code,
                    data.get("description", resp.text[:200]),
                )
                if resp.status_code >= 500 and attempt < max_retries:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                return False
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "Telegram sendMessage transport error (chat=%s attempt %d/%d): %s",
                    chat_id,
                    attempt,
                    max_retries,
                    mask_telegram_token(str(exc), self.config.bot_token),
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * attempt)
        return False

    async def send_markdown(
        self,
        content: str,
        *,
        chat_ids: list[str] | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        if not self.config.is_ready:
            return False

        targets = list(dict.fromkeys(chat_ids)) if chat_ids is not None else self._target_chat_ids()
        if not targets:
            return False

        html_text = markdown_to_telegram_html(content)
        html_chunks = split_telegram_message(html_text, max_len=4096)
        plain_text = _WECOM_FONT_RE.sub(r"\1", content)
        plain_chunks = split_telegram_message(plain_text, max_len=4096)

        delivered_any = False
        for cid in targets:
            chat_ok = True
            for idx, chunk in enumerate(html_chunks):
                plain_chunk = plain_chunks[idx] if idx < len(plain_chunks) else plain_text
                ok = await self._post_chunk(
                    cid,
                    chunk,
                    plain_chunk,
                    reply_to_message_id=reply_to_message_id if idx == 0 else None,
                )
                if not ok:
                    chat_ok = False
                    break
            if chat_ok:
                delivered_any = True
        return delivered_any

    async def _poll_loop(self) -> None:
        backoff_steps = (1.0, 3.0, 10.0)
        consecutive_errors = 0
        logger.info("Telegram getUpdates long-polling started")

        while self._running:
            try:
                params: dict[str, Any] = {
                    "timeout": self.config.poll_timeout_seconds,
                    "allowed_updates": ["message"],
                }
                if self._offset:
                    params["offset"] = self._offset

                resp = await self._client.get(
                    "/getUpdates",
                    params=params,
                    timeout=httpx.Timeout(self.config.poll_timeout_seconds + 15.0),
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(str(data.get("description") or "getUpdates returned ok=false"))

                consecutive_errors = 0
                updates = data.get("result") or []
                for update in updates:
                    if isinstance(update, dict):
                        uid = int(update.get("update_id", 0))
                        if uid >= self._offset:
                            self._offset = uid + 1
                        await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = backoff_steps[min(consecutive_errors, len(backoff_steps) - 1)]
                consecutive_errors += 1
                logger.warning(
                    "Telegram polling error (retry in %.1fs): %s",
                    delay,
                    mask_telegram_token(str(exc), self.config.bot_token),
                )
                await asyncio.sleep(delay)

    def _should_handle_group_message(self, message: dict[str, Any], text: str) -> bool:
        if not self.config.require_mention_in_group:
            return True

        # 1. Slash command (ignore commands explicitly addressed to a different bot)
        if text.startswith("/"):
            first_token = text.split(maxsplit=1)[0]
            if "@" in first_token and self._bot_username:
                target_bot = first_token.split("@", 1)[1].lower()
                return target_bot == self._bot_username.lower()
            return True

        # 2. Explicit @bot_username mention anywhere in message
        if self._bot_username and f"@{self._bot_username.lower()}" in text.lower():
            return True

        # 3. Direct reply to a message sent by this bot (great for replying "confirm")
        reply_to = message.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        reply_username = str(reply_from.get("username") or "").lower()
        if self._bot_username and reply_username == self._bot_username.lower():
            return True

        return False

    def _clean_bot_mention(self, text: str) -> str:
        if not self._bot_username:
            return text.strip()
        pattern = re.compile(rf"@{re.escape(self._bot_username)}\b", re.IGNORECASE)
        return pattern.sub("", text).strip()

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if self.command_handler is None:
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return

        from_user = message.get("from") or {}
        if from_user.get("is_bot"):
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        chat_type = str(chat.get("type") or "")
        message_id = message.get("message_id")
        user_id = str(from_user.get("id") or "").strip()

        if not chat_id:
            return

        if chat_type in {"group", "supergroup"} and not self._should_handle_group_message(message, text):
            return

        self.store.register_telegram_chat_id(chat_id)
        cleaned_text = self._clean_bot_mention(text)
        logger.info("Received Telegram message from user=%s chat=%s: %s", user_id, chat_id, cleaned_text)

        try:
            reply_md = await self.command_handler.handle(
                cleaned_text,
                user_id=user_id,
                channel="telegram",
            )
        except Exception as exc:
            logger.exception("Telegram command execution failed")
            reply_md = f"❌ Command error: {exc}"

        await self.send_markdown(
            reply_md,
            chat_ids=[chat_id],
            reply_to_message_id=int(message_id) if message_id is not None else None,
        )

from __future__ import annotations

import logging
from typing import Any

from wecom_aibot_sdk import WSClient, generate_req_id

from .commands import CommandHandler
from .config import AiBotConfig
from .store import StateStore

logger = logging.getLogger(__name__)


class WeComAiBotBridge:
    """Manages WeCom Smart Bot WebSocket long-connection for bidirectional group commands."""

    def __init__(
        self,
        *,
        config: AiBotConfig,
        store: StateStore,
        command_handler: CommandHandler,
    ) -> None:
        self.config = config
        self.store = store
        self.command_handler = command_handler
        self._client: WSClient | None = None
        self._authenticated = False

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected and self._authenticated)

    def _build_client(self) -> WSClient:
        client = WSClient(
            bot_id=self.config.bot_id,
            secret=self.config.secret,
            reconnect_interval=3000,
            max_reconnect_attempts=999999,
            heartbeat_interval=30000,
        )

        def _on_authenticated(*_: Any) -> None:
            self._authenticated = True
            logger.info("WeCom Smart Bot authenticated successfully")

        def _on_disconnected(*args: Any) -> None:
            self._authenticated = False
            logger.warning("WeCom Smart Bot disconnected: %s", args)

        def _on_error(err: Any) -> None:
            logger.error("WeCom Smart Bot error: %s", err)

        async def _on_text(frame: dict[str, Any]) -> None:
            await self._handle_text_frame(frame)

        client.on("authenticated", _on_authenticated)
        client.on("disconnected", _on_disconnected)
        client.on("error", _on_error)
        client.on("message.text", _on_text)

        return client

    async def _handle_text_frame(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        chat_id = str(body.get("chatid") or "").strip()
        from_info = body.get("from") or {}
        user_id = str(from_info.get("userid") or body.get("userid") or "").strip()
        text_content = str((body.get("text") or {}).get("content") or "").strip()

        if chat_id:
            self.store.register_chat_id(chat_id)

        logger.info("Received WeCom message from user=%s chat=%s: %s", user_id, chat_id, text_content)

        try:
            reply_md = await self.command_handler.handle(text_content, user_id=user_id)
        except Exception as exc:
            logger.exception("Command execution failed")
            reply_md = f"❌ 命令执行异常：{exc}"

        if self._client is None:
            return

        stream_id = generate_req_id("stream")
        try:
            await self._client.reply_stream(frame, stream_id, reply_md, finish=True)
        except Exception:
            logger.exception("Failed to send reply_stream to WeCom")

    async def start(self) -> None:
        if not self.config.is_ready:
            logger.info("WeCom Smart Bot not configured; skipping WebSocket long-connection")
            return
        self._client = self._build_client()
        await self._client.connect()

    async def stop(self) -> None:
        self._authenticated = False
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Error while disconnecting WeCom Smart Bot")
            self._client = None

    async def push_markdown(self, content: str) -> bool:
        """Optional proactive push to active chats via aibot_send_msg."""
        if not self.config.push_alerts or not self.is_connected or self._client is None:
            return False
        chat_ids = self.store.get_chat_ids()
        if not chat_ids:
            logger.warning("aibot push_alerts enabled, but no active chatid recorded yet (@bot once in group first)")
            return False

        delivered_any = False
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        for chat_id in chat_ids:
            try:
                await self._client.send_message(chat_id, payload)
                delivered_any = True
            except Exception as exc:
                logger.warning("aibot send_message failed for chat=%s: %s", chat_id, exc)
        return delivered_any

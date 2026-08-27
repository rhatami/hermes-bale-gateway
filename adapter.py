"""
Bale Messenger Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to Bale (بله) — an Iranian
messaging platform with a Telegram-compatible Bot API. Uses HTTP long-polling
(getUpdates) to receive messages and the standard Bot API to send responses.

Zero external dependencies beyond `requests` (already in Hermes's venv).

Configuration via environment variables:
    BALE_BOT_TOKEN          - Bot token (required)
    BALE_ALLOWED_USERS      - Comma-separated user IDs (optional)
    BALE_ALLOW_ALL_USERS    - Allow all users (optional, dev only)
    BALE_HOME_CHANNEL       - Default chat ID for cron delivery
    BALE_HOME_CHANNEL_NAME  - Display name for home channel
    BALE_POLL_INTERVAL      - Polling interval in seconds (default: 2)
    BALE_ACK_MESSAGE        - Instant "message received" ack text (empty = disabled)

Or via config.yaml:
    platforms:
      bale:
        enabled: true
        token: "123456:abcdef..."
        home_channel:
          platform: bale
          chat_id: "123456789"
          name: "My Bale Chat"
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)

logger = logging.getLogger(__name__)

# Bale API base URL (Telegram-compatible)
BALE_API_BASE = "https://tapi.bale.ai/bot"

# Max message length (Bale limit similar to Telegram)
MAX_MESSAGE_LENGTH = 4000


def _get_secret(name: str, default: str = "") -> str:
    """Read a secret from env with profile-aware scoping."""
    try:
        from agent.secret_scope import get_secret as _scoped_get_secret
        val = _scoped_get_secret(name, default)
        if val is not None:
            return val
    except Exception:
        pass
    return os.getenv(name, default).strip()


class BaleAdapter(BasePlatformAdapter):
    """Bale Messenger adapter using HTTP long-polling."""

    def __init__(self, config):
        # Add BALE to Platform enum if not already present
        if not hasattr(Platform, "BALE"):
            from enum import Enum
            Platform._value2member_map_["bale"] = Platform("bale")
            Platform.BALE = Platform("bale")
        platform = Platform.BALE
        super().__init__(config, platform)

        self.token = _get_secret("BALE_BOT_TOKEN", config.token or "")
        self.api_base = f"{BALE_API_BASE}{self.token}/"
        self.poll_interval = int(
            _get_secret("BALE_POLL_INTERVAL",
                        str(config.extra.get("poll_interval", 2)))
        )

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._offset: Optional[int] = None
        self._bot_id: Optional[int] = None
        self._last_poll = 0.0

        # Acknowledgment message sent right after a message is received
        # (Bale has no "is typing" indicator). Empty string disables it.
        self.ack_message = _get_secret(
            "BALE_ACK_MESSAGE",
            str(config.extra.get(
                "ack_message",
                "پیامت رسید 📩 دارم پردازشش می‌کنم، یه لحظه صبر کن…",
            )),
        )

        # chat_id -> ack message_id (edited with the real answer when ready)
        self._pending_acks: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Bale and verify the bot token."""
        if not self.token:
            logger.error("[Bale] BALE_BOT_TOKEN not set")
            return False

        try:
            import requests as req
            resp = await asyncio.to_thread(
                req.get, f"{self.api_base}getMe", timeout=10
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("[Bale] getMe failed: %s", data)
                return False

            bot = data["result"]
            self._bot_id = bot["id"]
            logger.info(
                "[Bale] Connected as @%s (id=%s)",
                bot.get("username", "?"), self._bot_id
            )

            # Start polling
            self._running = True
            self._poll_task = asyncio.ensure_future(self._poll_loop())
            return True

        except Exception as e:
            logger.error("[Bale] Connection failed: %s", e)
            return False

    async def disconnect(self) -> None:
        """Stop polling and disconnect."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[Bale] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a Bale chat.

        If there is a pending ack message for this chat (sent when the user's
        message arrived), the first chunk edits that ack in place instead of
        sending a new message. Remaining chunks are sent as new messages.
        """
        # Split long messages
        chunks = self._split_message(content, MAX_MESSAGE_LENGTH)
        sent_ids = []

        # Try to edit the pending ack with the first chunk (single-message UX)
        ack_id = self._pending_acks.pop(str(chat_id), None)
        edited_ack = False
        if ack_id and chunks and chunks[0].strip():
            edit_result = await self._api_call("editMessageText", {
                "chat_id": int(chat_id),
                "message_id": int(ack_id),
                "text": chunks[0],
            }, best_effort=True)
            if edit_result and edit_result.get("ok"):
                sent_ids.append(str(
                    edit_result.get("result", {}).get("message_id", ack_id)
                ))
                edited_ack = True

        start = 1 if edited_ack else 0
        for chunk in chunks[start:]:
            if not chunk.strip():
                continue
            payload = {"chat_id": int(chat_id), "text": chunk}
            if reply_to and not edited_ack:
                payload["reply_to_message_id"] = int(reply_to)

            result = await self._api_call("sendMessage", payload)
            if result and result.get("ok"):
                msg_id = result["result"]["message_id"]
                sent_ids.append(str(msg_id))
            else:
                return SendResult(
                    success=False,
                    error=result.get("description", "send failed") if result else "no response",
                )

        return SendResult(
            success=True,
            message_id=sent_ids[-1] if sent_ids else None,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator (not supported on Bale, best-effort)."""
        # Bale doesn't support sendChatAction, but try anyway
        await self._api_call("sendChatAction", {
            "chat_id": int(chat_id),
            "action": "typing",
        }, best_effort=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image by URL."""
        payload = {"chat_id": int(chat_id), "photo": image_url}
        if caption:
            payload["caption"] = caption[:1024]
        result = await self._api_call("sendPhoto", payload)
        if result and result.get("ok"):
            return SendResult(success=True)
        return SendResult(success=False, error="sendPhoto failed")

    async def get_chat_info(self, chat_id: str) -> dict:
        """Return basic chat info."""
        return {
            "chat_id": chat_id,
            "name": f"Bale chat {chat_id}",
            "type": "private",
        }

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        """Long-poll for new messages."""
        logger.info("[Bale] Polling started (interval=%ds)", self.poll_interval)

        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Bale] Poll error: %s", e)
                await asyncio.sleep(5)

        logger.info("[Bale] Polling stopped")

    async def _poll_once(self):
        """Fetch and process one batch of updates."""
        import requests as req

        params = {"timeout": 25}  # long-poll timeout
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            resp = await asyncio.to_thread(
                req.get,
                f"{self.api_base}getUpdates",
                params=params,
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            logger.debug("[Bale] Poll request failed: %s", e)
            await asyncio.sleep(self.poll_interval)
            return

        if not data.get("ok"):
            logger.warning("[Bale] getUpdates error: %s", data)
            await asyncio.sleep(self.poll_interval)
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            try:
                await self._process_update(update)
            except Exception as e:
                logger.error("[Bale] Update processing error: %s", e)

    async def _process_update(self, update: dict):
        """Process a single Bale update."""
        msg = update.get("message")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        user = msg.get("from", {})
        user_id = str(user.get("id", ""))
        text = msg.get("text", "") or msg.get("caption", "") or ""
        message_id = str(msg.get("message_id", ""))

        if not chat_id:
            return

        # Determine message type
        msg_type = MessageType.TEXT
        media_url = None

        if msg.get("photo"):
            msg_type = MessageType.IMAGE
            # Get largest photo
            photos = msg["photo"]
            if photos:
                media_url = photos[-1].get("file_id", "")
        elif msg.get("document"):
            msg_type = MessageType.DOCUMENT
            media_url = msg["document"].get("file_id", "")
        elif msg.get("voice"):
            msg_type = MessageType.VOICE
            media_url = msg["voice"].get("file_id", "")
        elif msg.get("audio"):
            msg_type = MessageType.AUDIO
            media_url = msg["audio"].get("file_id", "")
        elif msg.get("video"):
            msg_type = MessageType.VIDEO
            media_url = msg["video"].get("file_id", "")

        # Build source for session routing
        source = self.build_source(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user.get("first_name", ""),
            thread_id=None,
            message_id=message_id,

        )

        # Build event
        event = MessageEvent(
            source=source,
            text=text,
            message_type=msg_type,
            media_urls=[media_url] if media_url else [],
            reply_to_message_id=str(
                msg.get("reply_to_message", {}).get("message_id", "")
            ) or None,
            raw_message=msg,
        )

        # Send instant acknowledgment (Bale has no typing indicator).
        # Skip for: our own bot messages, empty updates, and slash commands.
        if self.ack_message:
            is_bot_msg = (
                user.get("is_bot", False)
                or (self._bot_id and str(user.get("id", "")) == str(self._bot_id))
            )
            has_content = bool(text.strip()) or bool(media_url)
            is_command = text.strip().startswith("/")
            if not is_bot_msg and has_content and not is_command:
                result = await self._api_call("sendMessage", {
                    "chat_id": int(chat_id),
                    "text": self.ack_message,
                }, best_effort=True)
                if result and result.get("ok"):
                    ack_id = result.get("result", {}).get("message_id")
                    if ack_id is not None:
                        self._pending_acks[str(chat_id)] = str(ack_id)

        await self.handle_message(event)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _api_call(
        self,
        method: str,
        payload: dict,
        best_effort: bool = False,
    ) -> Optional[dict]:
        """Call the Bale Bot API (non-blocking via thread)."""
        import requests as req

        def _do_request():
            return req.post(
                f"{self.api_base}{method}",
                json=payload,
                timeout=30,
            )

        try:
            resp = await asyncio.to_thread(_do_request)
            data = resp.json()
            if not data.get("ok") and not best_effort:
                logger.warning(
                    "[Bale] API %s failed: %s",
                    method, data.get("description", "?"),
                )
            return data
        except Exception as e:
            if not best_effort:
                logger.error("[Bale] API %s error: %s", method, e)
            return None

    @staticmethod
    def _split_message(text: str, max_len: int) -> List[str]:
        """Split a long message into chunks."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break

            # Try to split at newline
            idx = text.rfind("\n", 0, max_len)
            if idx < max_len // 2:
                # Try space
                idx = text.rfind(" ", 0, max_len)
            if idx < max_len // 2:
                # Hard split
                idx = max_len

            chunks.append(text[:idx])
            text = text[idx:].lstrip("\n")

        return chunks


# ---------------------------------------------------------------------------
# Plugin helpers
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check if Bale dependencies are available."""
    try:
        import requests
        return True
    except ImportError:
        logger.warning("[Bale] 'requests' package not available")
        return False


def validate_config(config) -> Optional[str]:
    """Validate Bale configuration. Returns None on success, error string on failure."""
    token = _get_secret("BALE_BOT_TOKEN", config.token if config else "")
    if not token:
        return "BALE_BOT_TOKEN not set"
    if ":" not in token:
        return "BALE_BOT_TOKEN format invalid (expected 'id:hash')"
    return True


def is_connected(adapter) -> bool:
    """Check if the adapter is connected."""
    return getattr(adapter, "_running", False)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig from env vars during gateway config load."""
    token = _get_secret("BALE_BOT_TOKEN")
    if not token:
        return None

    extra = {"token": token}

    poll = _get_secret("BALE_POLL_INTERVAL")
    if poll:
        extra["poll_interval"] = int(poll)

    # Home channel
    home_id = _get_secret("BALE_HOME_CHANNEL")
    home_name = _get_secret("BALE_HOME_CHANNEL_NAME", "Bale")
    if home_id:
        extra["home_channel"] = {
            "platform": "bale",
            "chat_id": home_id,
            "name": home_name,
        }

    return extra


async def _standalone_send(
    chat_id: str,
    text: str,
    config: Any = None,
    **kwargs,
) -> dict:
    """Out-of-process message delivery for cron jobs."""
    token = _get_secret("BALE_BOT_TOKEN")
    if not token:
        return {"success": False, "error": "BALE_BOT_TOKEN not set"}

    import requests as req
    try:
        resp = req.post(
            f"{BALE_API_BASE}{token}/sendMessage",
            json={"chat_id": int(chat_id), "text": text},
            timeout=30,
        )
        data = resp.json()
        return {"success": data.get("ok", False), "raw": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Platform registration
# ---------------------------------------------------------------------------

# Import Platform here to avoid circular imports at module level
try:
    from gateway.config import Platform
except ImportError:
    # Fallback for testing outside gateway
    class Platform:
        TELEGRAM = "telegram"


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="bale",
        label="Bale",
        adapter_factory=lambda cfg: BaleAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["BALE_BOT_TOKEN"],
        install_hint="No extra packages needed (requests is already installed)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BALE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BALE_ALLOWED_USERS",
        allow_all_env="BALE_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🇮🇷",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Bale (بله), an Iranian messaging platform "
            "with a Telegram-compatible API. Bale supports basic markdown "
            "(bold with *text* and links with [text](url)), but NOT GitHub-style "
            "markdown (no **bold**, no ## headers, no lists with -). "
            "Use plain text with occasional bold. Messages are limited to "
            "~4000 characters. Respond in the same language the user writes in."
        ),
    )

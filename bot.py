import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx
import socketio
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Regex to find markdown images: ![alt](url)
_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AGENT_ZERO_URL = os.environ.get("AGENT_ZERO_URL", "http://agent-zero:80")
AGENT_ZERO_API_KEY = os.environ["AGENT_ZERO_API_KEY"]
AGENT_ZERO_LOGIN = os.environ.get("AGENT_ZERO_LOGIN", "admin")
AGENT_ZERO_PASSWORD = os.environ.get("AGENT_ZERO_PASSWORD", "")
ALLOWED_USER_IDS: set[int] = set()

raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
if raw_ids.strip():
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))
DRAFT_THROTTLE_MS = int(os.environ.get("DRAFT_THROTTLE_MS", "200"))


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def context_id_for(chat_id: int) -> str:
    return f"telegram-{chat_id}"


# ---------------------------------------------------------------------------
# Agent Zero Client — HTTP + WebSocket streaming
# ---------------------------------------------------------------------------

@dataclass
class StreamState:
    """Tracks the latest response text and completion status from state_push events."""
    response_text: str = ""
    is_done: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class AgentZeroClient:
    """Communicates with Agent Zero via HTTP (message sending, reset) and
    Socket.IO WebSocket (streaming response via state_push)."""

    def __init__(self, base_url: str, api_key: str, login: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.login = login
        self.password = password

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Create an HTTP client with auth cookies from Agent Zero login."""
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        # Authenticate to get session cookies
        if self.login and self.password:
            try:
                await client.post(
                    f"{self.base_url}/login",
                    data={"username": self.login, "password": self.password},
                    follow_redirects=True,
                )
            except Exception:
                logger.debug("Login request failed, continuing with API key auth")
        return client

    async def send_message_blocking(self, context_id: str, text: str) -> str:
        """Fallback: send message via blocking /api_message endpoint."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/api_message",
                json={"message": text, "context_id": context_id},
                headers={"X-API-KEY": self.api_key},
            )
            response.raise_for_status()
            data = response.json()
        return data.get("response") or data.get("message") or str(data)

    async def send_message_streaming(self, context_id: str, text: str, attachments: list | None = None):
        """Send a message and yield (response_text, is_done) as the agent streams.

        Uses the web UI message path (/message_queue_add + /message_queue_send)
        combined with Socket.IO state_push subscription for streaming.
        """
        http_client = await self._get_http_client()
        stream_state = StreamState()

        # --- Set up Socket.IO client for /state_sync namespace ---
        sio = socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )

        # Get CSRF token for WebSocket handshake
        csrf_token = None
        try:
            resp = await http_client.get(f"{self.base_url}/csrf_token")
            if resp.status_code == 200:
                csrf_data = resp.json()
                csrf_token = csrf_data.get("token") or csrf_data.get("csrf_token")
        except Exception:
            logger.debug("Could not fetch CSRF token, proceeding without it")

        # Build connection headers (pass cookies from authenticated session)
        headers = {}
        if http_client.cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in http_client.cookies.items())
            headers["Cookie"] = cookie_str

        auth = {}
        if csrf_token:
            auth["csrf_token"] = csrf_token

        @sio.on("state_push", namespace="/state_sync")
        async def on_state_push(data):
            envelope = data if isinstance(data, dict) else {}
            # data might be the envelope itself or nested under "data"
            snapshot = envelope.get("snapshot") or envelope.get("data", {}).get("snapshot", {})
            if not snapshot:
                return

            # Extract response text from logs
            logs = snapshot.get("logs", [])
            response_parts = []
            for log_item in logs:
                if log_item.get("type") == "response":
                    content = log_item.get("content", "")
                    if content:
                        response_parts.append(content)

            if response_parts:
                stream_state.response_text = "\n\n".join(response_parts)
                stream_state.event.set()

            # Check if agent is done
            if not snapshot.get("log_progress_active", True):
                stream_state.is_done = True
                stream_state.event.set()

        # Connect to Socket.IO
        try:
            await sio.connect(
                self.base_url,
                namespaces=["/state_sync"],
                headers=headers,
                auth=auth if auth else None,
                wait_timeout=10,
            )
        except Exception:
            logger.warning("WebSocket connection failed, falling back to blocking API")
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True
            return

        # Subscribe to state updates for this context
        try:
            await sio.emit(
                "state_request",
                {
                    "context": context_id,
                    "log_from": 0,
                    "notifications_from": 0,
                    "timezone": "UTC",
                },
                namespace="/state_sync",
            )
        except Exception:
            logger.warning("Failed to send state_request")

        # Queue and send the message via HTTP (web UI path)
        try:
            await http_client.post(
                f"{self.base_url}/message_queue_add",
                json={"context": context_id, "text": text, "attachments": attachments or []},
                headers={"X-API-KEY": self.api_key},
            )
            await http_client.post(
                f"{self.base_url}/message_queue_send",
                json={"context": context_id, "send_all": True},
                headers={"X-API-KEY": self.api_key},
            )
        except Exception:
            logger.warning("message_queue path failed, trying /api_message via WebSocket fallback")
            await sio.disconnect()
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True
            return

        # Stream response chunks
        last_text = ""
        timeout_at = time.monotonic() + REQUEST_TIMEOUT
        try:
            while time.monotonic() < timeout_at:
                stream_state.event.clear()
                try:
                    await asyncio.wait_for(stream_state.event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    # No update in 2s — check if still connected
                    if not sio.connected:
                        break
                    continue

                current_text = stream_state.response_text
                if current_text != last_text:
                    last_text = current_text
                    yield current_text, stream_state.is_done

                if stream_state.is_done:
                    break

            # Final yield if we timed out but have text
            if not stream_state.is_done and last_text:
                yield last_text, True
        finally:
            try:
                await sio.disconnect()
            except Exception:
                pass
            await http_client.aclose()

    async def reset_chat(self, context_id: str) -> None:
        """Reset a conversation."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            await client.post(
                f"{self.base_url}/api_reset_chat",
                json={"context_id": context_id},
                headers={"X-API-KEY": self.api_key},
            )


# Global client instance
agent_client = AgentZeroClient(
    base_url=AGENT_ZERO_URL,
    api_key=AGENT_ZERO_API_KEY,
    login=AGENT_ZERO_LOGIN,
    password=AGENT_ZERO_PASSWORD,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def extract_images_from_response(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract markdown image references from response text.

    Returns (cleaned_text, [(alt_text, url), ...]).
    """
    images = _IMAGE_RE.findall(text)
    cleaned = _IMAGE_RE.sub('', text).strip()
    return cleaned, images


async def send_response_with_images(bot, chat_id: int, text: str) -> None:
    """Send a response that may contain markdown images as actual Telegram photos."""
    cleaned_text, images = extract_images_from_response(text)

    # Send each image as a Telegram photo
    for alt_text, url in images:
        # Resolve relative URLs against Agent Zero
        if url.startswith("/"):
            url = f"{AGENT_ZERO_URL}{url}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                image_bytes = resp.content
            caption = alt_text if alt_text else None
            await bot.send_photo(chat_id=chat_id, photo=image_bytes, caption=caption)
        except Exception:
            logger.warning("Failed to send image %s, sending URL as text", url)
            await bot.send_message(chat_id=chat_id, text=f"[Image: {alt_text or url}]({url})")

    # Send remaining text
    if cleaned_text:
        for i in range(0, len(cleaned_text), 4096):
            await bot.send_message(chat_id=chat_id, text=cleaned_text[i : i + 4096])


# ---------------------------------------------------------------------------
# Telegram Handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Hello! I'm a proxy to Agent Zero.\n\n"
        "Send me any message and I'll forward it to the agent.\n"
        "Use /reset to start a new conversation."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    ctx_id = context_id_for(update.effective_chat.id)
    try:
        await agent_client.reset_chat(ctx_id)
        await update.message.reply_text("Conversation has been reset. Send a new message to start fresh.")
    except Exception:
        logger.exception("Failed to reset chat")
        await update.message.reply_text("Failed to reset the conversation. Please try again.")


async def _stream_to_private_chat(
    bot, chat_id: int, ctx_id: str, text: str, attachments: list | None = None,
) -> None:
    """Stream response to a private chat using sendMessageDraft."""
    draft_id = int(time.time() * 1000) % (2**31 - 1)  # unique draft ID per message
    last_draft_time = 0.0
    throttle_sec = DRAFT_THROTTLE_MS / 1000.0
    final_text = ""

    try:
        async for response_text, is_done in agent_client.send_message_streaming(ctx_id, text, attachments):
            final_text = response_text
            now = time.monotonic()

            if is_done:
                break

            # Throttle draft updates
            if now - last_draft_time < throttle_sec:
                continue

            # Send draft (truncate to 4096 for Telegram limit)
            draft_text = response_text[:4096]
            if draft_text.strip():
                try:
                    await bot.send_message_draft(
                        chat_id=chat_id,
                        draft_id=draft_id,
                        text=draft_text,
                    )
                    last_draft_time = time.monotonic()
                except Exception:
                    logger.debug("send_message_draft failed, continuing")

    except Exception:
        logger.exception("Streaming failed")

    # Send final message (with image support)
    if not final_text or not final_text.strip():
        final_text = "(Agent Zero returned an empty response.)"

    await send_response_with_images(bot, chat_id, final_text)


async def _stream_to_group_chat(
    bot, chat_id: int, ctx_id: str, text: str, attachments: list | None = None,
) -> None:
    """Stream response to a group chat using sendMessage + editMessageText."""
    sent_message = None
    last_edit_time = 0.0
    edit_throttle_sec = 1.0  # Telegram rate-limits edits to ~1/sec
    final_text = ""

    try:
        async for response_text, is_done in agent_client.send_message_streaming(ctx_id, text, attachments):
            final_text = response_text
            now = time.monotonic()

            if is_done:
                break

            # Throttle edits
            if now - last_edit_time < edit_throttle_sec:
                continue

            preview_text = response_text[:4096]
            if not preview_text.strip():
                continue

            try:
                if sent_message is None:
                    sent_message = await bot.send_message(chat_id=chat_id, text=preview_text)
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=sent_message.message_id,
                        text=preview_text,
                    )
                last_edit_time = time.monotonic()
            except Exception:
                logger.debug("edit_message_text failed, continuing")

    except Exception:
        logger.exception("Streaming failed")

    if not final_text or not final_text.strip():
        final_text = "(Agent Zero returned an empty response.)"

    # Delete the streaming preview message before sending final response with images
    _, images = extract_images_from_response(final_text)
    if images and sent_message is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
            sent_message = None
        except Exception:
            logger.debug("Failed to delete preview message")

    if images:
        # Send final response using image-aware helper
        await send_response_with_images(bot, chat_id, final_text)
    else:
        # No images — do the normal final edit or send
        final_chunk = final_text[:4096]
        try:
            if sent_message is None:
                await bot.send_message(chat_id=chat_id, text=final_chunk)
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text=final_chunk,
                )
        except Exception:
            await bot.send_message(chat_id=chat_id, text=final_chunk)

        for i in range(4096, len(final_text), 4096):
            await bot.send_message(chat_id=chat_id, text=final_text[i : i + 4096])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    ctx_id = context_id_for(chat_id)
    is_private = update.effective_chat.type == ChatType.PRIVATE

    # Send typing indicator
    await update.effective_chat.send_action("typing")

    try:
        if is_private:
            await _stream_to_private_chat(context.bot, chat_id, ctx_id, user_text)
        else:
            await _stream_to_group_chat(context.bot, chat_id, ctx_id, user_text)
    except httpx.TimeoutException:
        await update.message.reply_text("Agent Zero took too long to respond. Please try again.")
    except httpx.ConnectError:
        await update.message.reply_text("Cannot reach Agent Zero. Is the service running?")
    except Exception:
        logger.exception("Error handling message")
        await update.message.reply_text("Something went wrong while contacting Agent Zero.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    ctx_id = context_id_for(chat_id)
    is_private = update.effective_chat.type == ChatType.PRIVATE

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    try:
        file = await photo.get_file()
        photo_bytes = await file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download photo from Telegram")
        await update.message.reply_text("Failed to download the photo. Please try again.")
        return

    # Encode as base64 data URI for Agent Zero
    b64_data = base64.b64encode(photo_bytes).decode("utf-8")
    attachment = {
        "path": f"data:image/jpeg;base64,{b64_data}",
        "name": f"photo_{photo.file_unique_id}.jpg",
    }

    user_text = update.message.caption or "(Photo sent)"

    await update.effective_chat.send_action("typing")

    try:
        if is_private:
            await _stream_to_private_chat(context.bot, chat_id, ctx_id, user_text, [attachment])
        else:
            await _stream_to_group_chat(context.bot, chat_id, ctx_id, user_text, [attachment])
    except httpx.TimeoutException:
        await update.message.reply_text("Agent Zero took too long to respond. Please try again.")
    except httpx.ConnectError:
        await update.message.reply_text("Cannot reach Agent Zero. Is the service running?")
    except Exception:
        logger.exception("Error handling photo message")
        await update.message.reply_text("Something went wrong while contacting Agent Zero.")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Telegram proxy bot starting (long-polling mode, streaming enabled)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

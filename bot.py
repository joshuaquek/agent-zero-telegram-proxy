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
# Regex to find markdown links: [text](url)
_LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
_VOICE_EXTENSIONS = {'.ogg', '.oga'}
_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.aac', '.m4a'} | _VOICE_EXTENSIONS
_DOCUMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.csv', '.zip', '.tar', '.gz', '.7z', '.rar',
    '.json', '.xml', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.css',
}


def _url_extension(url: str) -> str:
    """Return the lowercase file extension from a URL, ignoring query params."""
    path = url.split('?')[0].split('#')[0]
    dot = path.rfind('.')
    return path[dot:].lower() if dot != -1 else ""

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
            # If context not found, retry without context_id to auto-create
            if response.status_code == 404 and context_id:
                logger.info("Context %s not found, retrying without context_id", context_id)
                response = await client.post(
                    f"{self.base_url}/api_message",
                    json={"message": text, "context_id": ""},
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

        # Get CSRF token and runtime_id for WebSocket handshake
        csrf_token = None
        runtime_id = None
        try:
            resp = await http_client.get(f"{self.base_url}/csrf_token")
            if resp.status_code == 200:
                csrf_data = resp.json()
                csrf_token = csrf_data.get("token") or csrf_data.get("csrf_token")
                runtime_id = csrf_data.get("runtime_id")
        except Exception:
            logger.debug("Could not fetch CSRF token, proceeding without it")

        # Build connection headers (pass cookies from authenticated session)
        # Include the csrf_token cookie that Agent Zero's WebSocket middleware expects
        # Include Origin header required by Agent Zero's WebSocket origin validation
        headers = {"Origin": self.base_url}
        cookie_parts = []
        if http_client.cookies:
            cookie_parts = [f"{k}={v}" for k, v in http_client.cookies.items()]
        if csrf_token and runtime_id:
            cookie_parts.append(f"csrf_token_{runtime_id}={csrf_token}")
        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)

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
        # These endpoints require CSRF token (via header or cookie)
        queue_headers = {"X-API-KEY": self.api_key}
        if csrf_token:
            queue_headers["X-CSRF-Token"] = csrf_token
        try:
            # Ensure the context exists (create if needed)
            resp = await http_client.post(
                f"{self.base_url}/chat_create",
                json={"new_context": context_id},
                headers=queue_headers,
            )
            if resp.status_code == 200:
                logger.debug("Context %s ensured via chat_create", context_id)

            await http_client.post(
                f"{self.base_url}/message_queue_add",
                json={"context": context_id, "text": text, "attachments": attachments or []},
                headers=queue_headers,
            )
            await http_client.post(
                f"{self.base_url}/message_queue_send",
                json={"context": context_id, "send_all": True},
                headers=queue_headers,
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
# Media helpers
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """A media reference extracted from Agent Zero's response."""
    kind: str  # "image", "voice", "audio", "document"
    alt: str
    url: str


def extract_media_from_response(text: str) -> tuple[str, list[MediaItem]]:
    """Extract markdown image and file references from response text.

    Returns (cleaned_text, [MediaItem, ...]).
    """
    media: list[MediaItem] = []
    patterns_to_strip: list[re.Match] = []

    # 1) Markdown images: ![alt](url) — always treated as images
    for m in _IMAGE_RE.finditer(text):
        alt, url = m.group(1), m.group(2)
        ext = _url_extension(url)
        if ext in _VOICE_EXTENSIONS:
            media.append(MediaItem("voice", alt, url))
        elif ext in _AUDIO_EXTENSIONS:
            media.append(MediaItem("audio", alt, url))
        elif ext in _DOCUMENT_EXTENSIONS:
            media.append(MediaItem("document", alt, url))
        else:
            media.append(MediaItem("image", alt, url))
        patterns_to_strip.append(m)

    # 2) Markdown links: [text](url) — classify by extension
    for m in _LINK_RE.finditer(text):
        # Skip if this link was already captured as an image (![...](...)  contains [...](...))
        if any(im.start() < m.start() < im.end() for im in patterns_to_strip):
            continue
        alt, url = m.group(1), m.group(2)
        ext = _url_extension(url)
        if ext in _IMAGE_EXTENSIONS:
            media.append(MediaItem("image", alt, url))
            patterns_to_strip.append(m)
        elif ext in _VOICE_EXTENSIONS:
            media.append(MediaItem("voice", alt, url))
            patterns_to_strip.append(m)
        elif ext in _AUDIO_EXTENSIONS:
            media.append(MediaItem("audio", alt, url))
            patterns_to_strip.append(m)
        elif ext in _DOCUMENT_EXTENSIONS:
            media.append(MediaItem("document", alt, url))
            patterns_to_strip.append(m)

    # Strip matched patterns from the text (process from end to preserve positions)
    cleaned = text
    for m in sorted(patterns_to_strip, key=lambda x: x.start(), reverse=True):
        cleaned = cleaned[:m.start()] + cleaned[m.end():]
    cleaned = cleaned.strip()

    return cleaned, media


def _resolve_url(url: str) -> str:
    """Prepend AGENT_ZERO_URL to relative URLs."""
    if url.startswith("/"):
        return f"{AGENT_ZERO_URL}{url}"
    return url


async def _download_file(url: str) -> bytes:
    """Download a file from a URL and return its bytes."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def send_response_with_media(bot, chat_id: int, text: str) -> None:
    """Send a response that may contain markdown media as native Telegram media."""
    cleaned_text, media_items = extract_media_from_response(text)

    for item in media_items:
        url = _resolve_url(item.url)
        caption = item.alt if item.alt else None
        try:
            file_bytes = await _download_file(url)
            if item.kind == "image":
                await bot.send_photo(chat_id=chat_id, photo=file_bytes, caption=caption)
            elif item.kind == "voice":
                await bot.send_voice(chat_id=chat_id, voice=file_bytes, caption=caption)
            elif item.kind == "audio":
                await bot.send_audio(chat_id=chat_id, audio=file_bytes, caption=caption,
                                     title=item.alt or None)
            elif item.kind == "document":
                filename = item.alt or item.url.split('/')[-1].split('?')[0]
                await bot.send_document(chat_id=chat_id, document=file_bytes,
                                        filename=filename, caption=caption)
        except Exception:
            logger.warning("Failed to send %s %s, sending as text link", item.kind, url)
            await bot.send_message(chat_id=chat_id, text=f"[{item.alt or item.kind}: {url}]({url})")

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

    await send_response_with_media(bot, chat_id, final_text)


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
    _, media_items = extract_media_from_response(final_text)
    if media_items and sent_message is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
            sent_message = None
        except Exception:
            logger.debug("Failed to delete preview message")

    if media_items:
        # Send final response using image-aware helper
        await send_response_with_media(bot, chat_id, final_text)
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


async def _handle_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    attachment: dict, caption_fallback: str,
) -> None:
    """Shared logic for forwarding a media attachment to Agent Zero."""
    chat_id = update.effective_chat.id
    ctx_id = context_id_for(chat_id)
    is_private = update.effective_chat.type == ChatType.PRIVATE
    user_text = update.message.caption or caption_fallback

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
        logger.exception("Error handling media message")
        await update.message.reply_text("Something went wrong while contacting Agent Zero.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    photo = update.message.photo[-1]
    try:
        file = await photo.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download photo from Telegram")
        await update.message.reply_text("Failed to download the photo. Please try again.")
        return

    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    attachment = {
        "path": f"data:image/jpeg;base64,{b64_data}",
        "name": f"photo_{photo.file_unique_id}.jpg",
    }
    await _handle_media(update, context, attachment, "(Photo sent)")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    doc = update.message.document
    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download document from Telegram")
        await update.message.reply_text("Failed to download the document. Please try again.")
        return

    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    mime_type = doc.mime_type or "application/octet-stream"
    filename = doc.file_name or f"document_{doc.file_unique_id}"
    attachment = {
        "path": f"data:{mime_type};base64,{b64_data}",
        "name": filename,
    }
    await _handle_media(update, context, attachment, f"(Document sent: {filename})")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    voice = update.message.voice
    try:
        file = await voice.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download voice message from Telegram")
        await update.message.reply_text("Failed to download the voice message. Please try again.")
        return

    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    mime_type = voice.mime_type or "audio/ogg"
    attachment = {
        "path": f"data:{mime_type};base64,{b64_data}",
        "name": f"voice_{voice.file_unique_id}.ogg",
    }
    await _handle_media(update, context, attachment, "(Voice message sent)")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Telegram proxy bot starting (long-polling mode, streaming enabled)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

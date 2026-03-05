"""Telegram command and message handlers."""

import base64
import time

import httpx
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from agent_client import AgentZeroClient
from config import (
    AGENT_ZERO_API_KEY,
    AGENT_ZERO_LOGIN,
    AGENT_ZERO_PASSWORD,
    AGENT_ZERO_URL,
    DRAFT_THROTTLE_MS,
    context_id_for,
    is_allowed,
    logger,
)
from md_to_html import md_to_tg_html, safe_md_to_tg_html
from media import extract_media_from_response, send_response_with_media
from telegram_send import edit_html_message, send_html_message, split_html_chunks

# Global client instance
agent_client = AgentZeroClient(
    base_url=AGENT_ZERO_URL,
    api_key=AGENT_ZERO_API_KEY,
    login=AGENT_ZERO_LOGIN,
    password=AGENT_ZERO_PASSWORD,
)


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
    draft_id = int(time.time() * 1000) % (2**31 - 1)
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

            # Send draft — convert to HTML only if tags are balanced
            draft_converted, draft_is_html = safe_md_to_tg_html(response_text)
            draft_text = draft_converted[:4096]
            if draft_text.strip():
                try:
                    kwargs = {"chat_id": chat_id, "draft_id": draft_id, "text": draft_text}
                    if draft_is_html:
                        kwargs["parse_mode"] = ParseMode.HTML
                    await bot.send_message_draft(**kwargs)
                    last_draft_time = time.monotonic()
                except Exception:
                    logger.debug("send_message_draft failed, continuing")

    except Exception:
        logger.exception("Streaming failed")

    # Send final message — pass draft_id so Telegram replaces the draft preview
    if not final_text or not final_text.strip():
        final_text = "(Agent Zero returned an empty response.)"

    await send_response_with_media(bot, chat_id, final_text, draft_id=draft_id)


async def _stream_to_group_chat(
    bot, chat_id: int, ctx_id: str, text: str, attachments: list | None = None,
) -> None:
    """Stream response to a group chat using sendMessage + editMessageText."""
    sent_message = None
    last_edit_time = 0.0
    edit_throttle_sec = 1.0
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

            preview_converted, preview_is_html = safe_md_to_tg_html(response_text)
            preview_text = preview_converted[:4096]
            if not preview_text.strip():
                continue

            parse_kw = {"parse_mode": ParseMode.HTML} if preview_is_html else {}
            try:
                if sent_message is None:
                    sent_message = await bot.send_message(chat_id=chat_id, text=preview_text, **parse_kw)
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=sent_message.message_id,
                        text=preview_text, **parse_kw,
                    )
                last_edit_time = time.monotonic()
            except Exception:
                logger.debug("preview message send/edit failed, continuing")

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
        await send_response_with_media(bot, chat_id, final_text)
    else:
        # No images — do the normal final edit or send
        final_html = md_to_tg_html(final_text)
        chunks = split_html_chunks(final_html)
        if chunks:
            first_chunk = chunks[0]
            if sent_message is not None:
                ok = await edit_html_message(bot, chat_id, sent_message.message_id, first_chunk)
                if not ok:
                    await send_html_message(bot, chat_id, first_chunk)
            else:
                await send_html_message(bot, chat_id, first_chunk)
            for chunk in chunks[1:]:
                await send_html_message(bot, chat_id, chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    ctx_id = context_id_for(chat_id)
    is_private = update.effective_chat.type == ChatType.PRIVATE

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

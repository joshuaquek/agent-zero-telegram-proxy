"""Helpers for sending Telegram messages with HTML formatting and safe chunking."""

import re

from telegram.constants import ParseMode

from config import logger


def split_html_chunks(html_text: str, max_len: int = 4096) -> list[str]:
    """Split HTML text into chunks that respect Telegram's size limit.

    Avoids splitting inside HTML tags or entities.  Falls back to hard
    truncation only as a last resort.
    """
    if len(html_text) <= max_len:
        return [html_text] if html_text.strip() else []

    chunks: list[str] = []
    remaining = html_text
    while remaining:
        if len(remaining) <= max_len:
            if remaining.strip():
                chunks.append(remaining)
            break

        # Find a safe split point: prefer newline, then space, before max_len
        cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len

        # Make sure we don't cut inside an HTML tag
        open_angle = remaining.rfind("<", 0, cut)
        close_angle = remaining.rfind(">", 0, cut)
        if open_angle > close_angle:
            # We're inside a tag — back up to before it
            cut = open_angle

        # Also avoid cutting inside an HTML entity (&amp; etc.)
        amp = remaining.rfind("&", max(0, cut - 10), cut)
        if amp != -1 and ";" not in remaining[amp:cut]:
            cut = amp

        chunk = remaining[:cut]
        if chunk.strip():
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")

    return chunks


async def send_html_message(bot, chat_id: int, text: str, *, draft_id: int | None = None):
    """Send a single message with parse_mode=HTML, falling back to plain text on error.

    *draft_id* is passed via ``api_kwargs`` so Telegram replaces the streaming
    draft preview with the final message (Bot API 9.5+).
    """
    api_kw = {"draft_id": draft_id} if draft_id else None
    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            api_kwargs=api_kw,
        )
        logger.info("[send_html] OK, sent %d chars with HTML parse_mode", len(text))
        return msg
    except Exception as exc:
        logger.warning("[send_html] HTML send failed (%s), falling back to plain text. Text sample: %s",
                       exc, text[:200])
        try:
            return await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Plain text send also failed")
            return None


async def edit_html_message(bot, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
    """Edit a message with parse_mode=HTML, falling back to plain text on error."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode=ParseMode.HTML, **kwargs,
        )
        return True
    except Exception:
        logger.warning("HTML edit failed, falling back to plain text")
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, **kwargs,
            )
            return True
        except Exception:
            logger.debug("Plain text edit also failed")
            return False


async def send_html_chunks(
    bot, chat_id: int, html_text: str, *, draft_id: int | None = None,
) -> None:
    """Split HTML into safe chunks and send each one.

    *draft_id* is passed only to the first chunk so Telegram replaces the
    streaming draft preview with the final message.
    """
    chunks = split_html_chunks(html_text)
    for i, chunk in enumerate(chunks):
        await send_html_message(bot, chat_id, chunk, draft_id=draft_id if i == 0 else None)

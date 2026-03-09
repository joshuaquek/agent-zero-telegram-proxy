"""Media extraction, download, and sending helpers."""

import html as html_mod
import re
from dataclasses import dataclass

import httpx

from config import AGENT_ZERO_LOGIN, AGENT_ZERO_PASSWORD, AGENT_ZERO_URL, logger
from md_to_html import md_to_tg_html
from telegram_send import send_html_chunks, send_html_message

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
_VOICE_EXTENSIONS = {'.ogg', '.oga'}
_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.aac', '.m4a'} | _VOICE_EXTENSIONS
_DOCUMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.csv', '.zip', '.tar', '.gz', '.7z', '.rar',
    '.json', '.xml', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.css',
}
_ALL_KNOWN_EXTENSIONS = _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS | _DOCUMENT_EXTENSIONS

# Regex to find markdown images: ![alt](url)
_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
# Regex to find markdown links: [text](url)
_LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
# Regex to find plain-text Agent Zero file paths: /a0/usr/<subpath>/<file>.<ext>
_A0_PATH_RE = re.compile(r'/a0/usr/(\S+\.\w+)')

# Trivial leftover text after media extraction that should be suppressed
# (e.g. Agent Zero responds with just "image" and a screenshot path in logs)
_TRIVIAL_MEDIA_LABELS = {
    'image', 'images', 'photo', 'photos', 'picture', 'pictures',
    'screenshot', 'screenshots', 'file', 'files', 'document', 'documents',
    'audio', 'voice', 'video',
}


def _url_extension(url: str) -> str:
    """Return the lowercase file extension from a URL, ignoring query params."""
    path = url.split('?')[0].split('#')[0]
    dot = path.rfind('.')
    return path[dot:].lower() if dot != -1 else ""


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

    # 1) Markdown images: ![alt](url)
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

    # 3) Plain-text Agent Zero container paths: /a0/usr/chats/.../<file>.<ext>
    #    Keep the full /a0/usr/ prefix so _resolve_url routes through /image_get.
    #    Skip if the same filename was already extracted from a markdown reference.
    existing_filenames = {item.url.split("/")[-1].split("?")[0].split("&")[0] for item in media}
    for m in _A0_PATH_RE.finditer(text):
        if any(im.start() <= m.start() < im.end() for im in patterns_to_strip):
            continue
        full_path = "/a0/usr/" + m.group(1)  # e.g. /a0/usr/screenshots/screenshot.png
        filename = full_path.split("/")[-1]
        if filename in existing_filenames:
            patterns_to_strip.append(m)  # still strip from text
            continue
        ext = _url_extension(full_path)
        if ext not in _ALL_KNOWN_EXTENSIONS:
            continue
        if ext in _IMAGE_EXTENSIONS:
            media.append(MediaItem("image", "", full_path))
        elif ext in _VOICE_EXTENSIONS:
            media.append(MediaItem("voice", "", full_path))
        elif ext in _AUDIO_EXTENSIONS:
            media.append(MediaItem("audio", "", full_path))
        elif ext in _DOCUMENT_EXTENSIONS:
            media.append(MediaItem("document", "", full_path))
        existing_filenames.add(filename)
        patterns_to_strip.append(m)

    # Strip matched patterns from the text
    cleaned = text
    for m in sorted(patterns_to_strip, key=lambda x: x.start(), reverse=True):
        cleaned = cleaned[:m.start()] + cleaned[m.end():]
    cleaned = cleaned.strip()

    return cleaned, media


def _resolve_url(url: str) -> str:
    """Resolve a media URL to a fully-qualified HTTP URL on Agent Zero.

    Agent Zero uses ``img:///a0/usr/...`` for screenshots.  Its web UI
    translates that to ``/image_get?path=<path>``.  We do the same here.
    Plain ``/a0/usr/...`` paths are also routed through ``/image_get``.
    """
    # img:///a0/usr/path&t=... → strip scheme and optional cache-buster
    if url.startswith("img://"):
        path = url[len("img://"):]
        # Strip trailing cache-buster (&t=...)
        if "&" in path:
            path = path.split("&")[0]
        return f"{AGENT_ZERO_URL}/image_get?path={path}"
    # /a0/usr/screenshots/... → use image_get endpoint
    if url.startswith("/a0/usr/") or url.startswith("/a0/"):
        return f"{AGENT_ZERO_URL}/image_get?path={url}"
    if url.startswith("/"):
        return f"{AGENT_ZERO_URL}{url}"
    return url


async def _download_file(url: str) -> bytes:
    """Download a file from a URL and return its bytes."""
    async with httpx.AsyncClient(timeout=30) as client:
        resolved = _resolve_url(url)
        headers: dict[str, str] = {}
        if resolved.startswith(AGENT_ZERO_URL) and AGENT_ZERO_LOGIN and AGENT_ZERO_PASSWORD:
            try:
                await client.post(
                    f"{AGENT_ZERO_URL}/login",
                    data={"username": AGENT_ZERO_LOGIN, "password": AGENT_ZERO_PASSWORD},
                    follow_redirects=True,
                )
            except Exception:
                logger.debug("Login for file download failed, trying without auth")
            # Fetch CSRF token — required by endpoints like /image_get
            try:
                csrf_resp = await client.get(f"{AGENT_ZERO_URL}/csrf_token")
                if csrf_resp.status_code == 200:
                    csrf_data = csrf_resp.json()
                    token = csrf_data.get("token") or csrf_data.get("csrf_token")
                    runtime_id = csrf_data.get("runtime_id")
                    if token:
                        headers["X-CSRF-Token"] = token
                    if token and runtime_id:
                        client.cookies.set(f"csrf_token_{runtime_id}", token)
            except Exception:
                logger.debug("CSRF token fetch failed, trying without it")
        resp = await client.get(resolved, headers=headers)
        resp.raise_for_status()
        return resp.content


async def send_response_with_media(
    bot, chat_id: int, text: str, *,
    draft_id: int | None = None, message_thread_id: int | None = None,
) -> None:
    """Send a response that may contain markdown media as native Telegram media.

    If *draft_id* is provided, the first text message sent will include it so
    that Telegram replaces the streaming draft preview with the final message.
    *message_thread_id* targets a specific forum topic.
    """
    cleaned_text, media_items = extract_media_from_response(text)
    if len(cleaned_text) != len(text.strip()):
        logger.warning("[send_response_with_media] text shrunk: input=%d, cleaned=%d, media=%d items, tail=%r",
                       len(text), len(cleaned_text), len(media_items), text[len(cleaned_text):len(cleaned_text)+100])

    thread_kw = {"message_thread_id": message_thread_id} if message_thread_id is not None else {}
    for item in media_items:
        resolved_url = _resolve_url(item.url)
        caption = item.alt if item.alt else None
        try:
            file_bytes = await _download_file(item.url)
            if item.kind == "image":
                await bot.send_photo(chat_id=chat_id, photo=file_bytes, caption=caption, **thread_kw)
            elif item.kind == "voice":
                await bot.send_voice(chat_id=chat_id, voice=file_bytes, caption=caption, **thread_kw)
            elif item.kind == "audio":
                await bot.send_audio(chat_id=chat_id, audio=file_bytes, caption=caption,
                                     title=item.alt or None, **thread_kw)
            elif item.kind == "document":
                filename = item.alt or item.url.split('/')[-1].split('?')[0]
                await bot.send_document(chat_id=chat_id, document=file_bytes,
                                        filename=filename, caption=caption, **thread_kw)
        except Exception:
            logger.exception("Failed to send %s %s, sending as text link", item.kind, resolved_url)
            link_html = f'<a href="{html_mod.escape(resolved_url)}">{html_mod.escape(item.alt or item.kind)}</a>'
            await send_html_message(bot, chat_id, link_html, message_thread_id=message_thread_id)

    # Send remaining text with Telegram HTML formatting
    # Skip trivial leftover labels like "image" when we already sent media
    if cleaned_text and media_items and cleaned_text.strip().lower() in _TRIVIAL_MEDIA_LABELS:
        logger.info("[send_response] Suppressing trivial leftover text %r (sent %d media items)",
                     cleaned_text, len(media_items))
        cleaned_text = ""

    if cleaned_text:
        formatted = md_to_tg_html(cleaned_text)
        logger.info("[send_response] raw=%d chars, html=%d chars, sample: %s",
                     len(cleaned_text), len(formatted), formatted[:300])
        await send_html_chunks(bot, chat_id, formatted, draft_id=draft_id, message_thread_id=message_thread_id)

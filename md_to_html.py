"""Native Markdown to Telegram HTML converter.

Self-contained, no third-party dependencies — uses only stdlib ``re`` and ``html``.
"""

import html as html_mod
import re

from config import logger

_CODE_BLOCK_RE = re.compile(
    r"(?P<fence>`{3,})(?P<lang>\w+)?\n?(?P<code>[\s\S]*?)(?<=\n)?(?P=fence)",
    flags=re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

_BOLD_RE = re.compile(r"(?<!\\)\*\*(?=\S)(.*?)(?<=\S)\*\*", re.DOTALL)
_UNDERLINE_RE = re.compile(
    r"(?<!\\)(?<![A-Za-z0-9_])__(?=\S)(.*?)(?<=\S)__(?![A-Za-z0-9_])", re.DOTALL,
)
_ITALIC_UNDERSCORE_RE = re.compile(
    r"(?<!\\)(?<![A-Za-z0-9_])_(?=\S)(.*?)(?<=\S)_(?![A-Za-z0-9_])", re.DOTALL,
)
_ITALIC_STAR_RE = re.compile(
    r"(?<![A-Za-z0-9\\])\*(?!\*)(?=[^\s])(.*?)(?<![\s\\])\*(?![A-Za-z0-9\\])", re.DOTALL,
)
_STRIKETHROUGH_RE = re.compile(r"(?<!\\)~~(?=\S)(.*?)(?<=\S)~~", re.DOTALL)
_SPOILER_RE = re.compile(r"(?<!\\)\|\|(?=\S)([^\n]*?)(?<=\S)\|\|")

_INLINE_PATTERN_MAP: dict[str, re.Pattern] = {
    "**": _BOLD_RE,
    "__": _UNDERLINE_RE,
    "_": _ITALIC_UNDERSCORE_RE,
    "~~": _STRIKETHROUGH_RE,
    "||": _SPOILER_RE,
}


def _ensure_closing_delimiters(text: str) -> str:
    """Append missing closing backtick fences so code blocks always close."""
    open_fence = None
    for line in text.splitlines():
        stripped = line.strip()
        if open_fence is None:
            m = re.match(r"^(?P<fence>`{3,})(?P<lang>\w+)?$", stripped)
            if m:
                open_fence = m.group("fence")
        else:
            if stripped.endswith(open_fence):
                open_fence = None

    if open_fence is not None:
        if not text.endswith("\n"):
            text += "\n"
        text += open_fence

    cleaned = _CODE_BLOCK_RE.sub("", text)
    if cleaned.count("```") % 2 != 0:
        text += "```"

    # Count unescaped single backticks
    cleaned = _CODE_BLOCK_RE.sub("", text)
    count = 0
    for i, ch in enumerate(cleaned):
        if ch != "`":
            continue
        bs = 0
        j = i - 1
        while j >= 0 and cleaned[j] == "\\":
            bs += 1
            j -= 1
        if bs % 2 == 0:
            count += 1
    if count % 2 != 0:
        text += "`"

    return text


def _extract_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code blocks with placeholders; return (text, {placeholder: html})."""
    text = _ensure_closing_delimiters(text)
    placeholders: list[str] = []
    blocks: dict[str, str] = {}
    pattern = re.compile(
        r"(?P<fence>`{3,})(?P<lang>\w+)?\n?(?P<code>[\s\S]*?)(?<=\n)?(?P=fence)",
        flags=re.DOTALL,
    )
    modified = text
    for m in pattern.finditer(text):
        lang = m.group("lang") or ""
        code = html_mod.escape(m.group("code"))
        ph = f"CODEBLOCKPLACEHOLDER_{len(placeholders)}_"
        placeholders.append(ph)
        if lang:
            blocks[ph] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            blocks[ph] = f"<pre><code>{code}</code></pre>"
        modified = modified.replace(m.group(0), ph, 1)
    return modified, blocks


def _combine_blockquotes(text: str) -> str:
    """Collapse consecutive markdown blockquote lines into Telegram <blockquote> HTML."""
    lines = text.split("\n")
    combined: list[str] = []
    bq_lines: list[str] = []
    in_bq = False
    expandable = False

    for line in lines:
        if line.startswith("**>"):
            in_bq = True
            expandable = True
            bq_lines.append(line[3:].strip())
        elif line.startswith(">**") and (len(line) == 3 or line[3].isspace()):
            in_bq = True
            expandable = True
            bq_lines.append(line[3:].strip())
        elif line.startswith(">"):
            if not in_bq:
                in_bq = True
                expandable = False
            bq_lines.append(line[1:].strip())
        else:
            if in_bq:
                tag = "blockquote expandable" if expandable else "blockquote"
                combined.append(f"<{tag}>" + "\n".join(bq_lines) + "</blockquote>")
                bq_lines = []
                in_bq = False
                expandable = False
            combined.append(line)

    if in_bq:
        tag = "blockquote expandable" if expandable else "blockquote"
        combined.append(f"<{tag}>" + "\n".join(bq_lines) + "</blockquote>")

    return "\n".join(combined)


def _extract_inline_code(text: str) -> tuple[str, dict[str, str]]:
    """Replace inline `code` with placeholders; return (text, {placeholder: raw})."""
    placeholders: list[str] = []
    snippets: dict[str, str] = {}

    def _repl(m: re.Match) -> str:
        ph = f"INLINECODEPLACEHOLDER_{len(placeholders)}_"
        placeholders.append(ph)
        snippets[ph] = m.group(1)
        return ph

    return _INLINE_CODE_RE.sub(_repl, text), snippets


def _split_by_tag(text: str, md_tag: str, html_tag: str) -> str:
    """Convert a markdown delimiter pair to the corresponding HTML tag."""
    pattern = _INLINE_PATTERN_MAP.get(md_tag)
    if pattern is None:
        escaped = re.escape(md_tag)
        pattern = re.compile(rf"(?<!\\){escaped}(?=\S)(.*?)(?<=\S){escaped}", re.DOTALL)

    def _wrap(m: re.Match) -> str:
        inner = m.group(1)
        if html_tag == 'span class="tg-spoiler"':
            return f'<span class="tg-spoiler">{inner}</span>'
        return f"<{html_tag}>{inner}</{html_tag}>"

    return pattern.sub(_wrap, text)


def md_to_tg_html(text: str) -> str:
    """Convert markdown text to Telegram-compatible HTML.

    Handles: code blocks, inline code, bold, italic, underline,
    strikethrough, spoiler, blockquotes, headings, list bullets, and links.
    Falls back to HTML-escaped plain text if conversion fails.
    """
    try:
        out, block_map = _extract_code_blocks(text)
        out = _combine_blockquotes(out)
        out, inline_snippets = _extract_inline_code(out)

        # Escape HTML entities in non-code text
        out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Headings -> bold
        out = re.sub(r"^(#{1,6})\s+(.+)$", r"<b>\2</b>", out, flags=re.MULTILINE)
        # List bullets
        out = re.sub(r"^(\s*)[\-\*]\s+(.+)$", r"\1• \2", out, flags=re.MULTILINE)

        # Combined bold+italic / underline+italic
        out = re.sub(r"\*\*\*(.*?)\*\*\*", r"<b><i>\1</i></b>", out)
        out = re.sub(r"\_\_\_(.*?)\_\_\_", r"<u><i>\1</i></u>", out)

        # Individual formatting
        out = _split_by_tag(out, "**", "b")
        out = _split_by_tag(out, "__", "u")
        out = _split_by_tag(out, "~~", "s")
        out = _split_by_tag(out, "||", 'span class="tg-spoiler"')
        out = _ITALIC_STAR_RE.sub(r"<i>\1</i>", out)
        out = _split_by_tag(out, "_", "i")

        # Remove citation markers (e.g. from ChatGPT)
        out = re.sub(r"【[^】]+】", "", out)

        # Markdown links -> HTML links (handles both [text](url) and ![alt](url))
        out = re.sub(
            r"(?:!?)\[((?:[^\[\]]|\[.*?\])*)\]\(([^)]+)\)",
            r'<a href="\2">\1</a>',
            out,
        )

        # Reinsert inline code with HTML escaping
        for ph, snippet in inline_snippets.items():
            escaped = html_mod.escape(snippet)
            out = out.replace(ph, f"<code>{escaped}</code>")

        # Reinsert code blocks
        for ph, html_block in block_map.items():
            out = out.replace(ph, html_block, 1)

        # Unescape blockquote/spoiler tags that got HTML-escaped
        out = (
            out.replace("&lt;blockquote&gt;", "<blockquote>")
            .replace("&lt;/blockquote&gt;", "</blockquote>")
            .replace("&lt;blockquote expandable&gt;", "<blockquote expandable>")
            .replace('&lt;span class="tg-spoiler"&gt;', '<span class="tg-spoiler">')
            .replace("&lt;/span&gt;", "</span>")
        )

        # Collapse excessive newlines
        out = re.sub(r"\n{3,}", "\n\n", out)

        return out.strip()
    except Exception:
        logger.debug("Markdown->HTML conversion failed, falling back to escaped plain text")
        return html_mod.escape(text)


def has_balanced_tags(text: str) -> bool:
    """Check whether all HTML tags in *text* are properly opened and closed."""
    tag_re = re.compile(r"<(/?)(\w+)[^>]*?>")
    stack: list[str] = []
    for m in tag_re.finditer(text):
        is_close, tag_name = m.group(1), m.group(2).lower()
        if is_close:
            if not stack or stack[-1] != tag_name:
                return False
            stack.pop()
        else:
            stack.append(tag_name)
    return len(stack) == 0


def safe_md_to_tg_html(text: str) -> tuple[str, bool]:
    """Convert markdown to Telegram HTML only if the result has balanced tags.

    Returns ``(converted_text, used_html)`` — *used_html* is ``True`` when
    the HTML conversion succeeded and the tags are balanced (safe to send
    with ``parse_mode=HTML``), ``False`` when the caller should send as
    plain text instead.
    """
    html_text = md_to_tg_html(text)
    if has_balanced_tags(html_text):
        return html_text, True
    return text, False

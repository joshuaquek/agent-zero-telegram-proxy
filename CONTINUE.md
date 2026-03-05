# Session Handoff — Telegram Proxy for Agent Zero

## Current State (2026-03-05)

**Latest commit:** `97627f9` — baseline log count fix for state_push history pollution
**Branch:** `main`
**Uncommitted:** Source files moved into `src/` directory; Dockerfile and docs updated to match.
**Deployed:** Needs redeployment to test latest fixes.

## Recent Changes

### Source Reorganization (uncommitted)
All Python source files moved from project root into `src/`:
- `bot.py`, `config.py`, `handlers.py`, `agent_client.py`, `md_to_html.py`, `media.py`, `telegram_send.py`
- Dockerfile updated: `COPY src/ ./src/` and `CMD ["python", "src/bot.py"]`
- Docs updated with new paths

### Previous Session Work (committed)

1. **Native Markdown-to-Telegram HTML Converter** (`src/md_to_html.py`)
   - Replaced `chatgpt-md-converter` with self-contained implementation (no third-party deps)
   - Handles bold, italic, code blocks, inline code, links, blockquotes, headings, list bullets, strikethrough, spoiler
   - HTML entity escaping, unclosed code fence auto-repair, fallback to escaped plain text

2. **Telegram HTML Sending Helpers** (`src/telegram_send.py`)
   - `send_html_message()` / `edit_html_message()` with plain-text fallback
   - `split_html_chunks()` — safe splitting that never breaks inside `<tag>` or `&entity;`
   - `safe_md_to_tg_html()` — only returns HTML when tags are balanced

3. **Draft-to-Final Message Fix** — `draft_id` threaded through streaming pipeline so Telegram replaces the draft preview with the final formatted message

4. **Historical Log Pollution Fix** (`97627f9`) — First `state_push` sets `baseline_log_count`, subsequent pushes only process new logs via `logs[baseline_log_count:]`

5. **Modularization** — Original monolithic `bot.py` (1103 lines) split into 7 focused modules

## What Needs Testing After Redeployment

1. Bold/italic formatting renders correctly (not raw `**` markers)
2. No garbled history from old log concatenation
3. Private chat: draft preview transitions smoothly to final formatted message
4. Code blocks render as `<pre><code>` in Telegram
5. Long responses (>4096 chars) split cleanly without breaking mid-tag

## Known Issues

- **Cognitive complexity** — `src/agent_client.py:send_message_streaming` is flagged by linter (complexity 52 vs 15 allowed). Could be refactored.
- **`draft_id` compatibility** — If `python-telegram-bot` v22.6 doesn't support `draft_id` on `sendMessage`, the fallback catches it. Check logs for `[send_html] HTML send failed`.
- **Streaming HTML safety** — During streaming, incomplete markdown stays as plain text until closing markers arrive (intentional via `safe_md_to_tg_html`).

## File Structure

```
├── src/
│   ├── bot.py              # Entrypoint
│   ├── config.py           # Configuration & env vars
│   ├── md_to_html.py       # Markdown → Telegram HTML
│   ├── telegram_send.py    # Safe HTML send/edit/chunk helpers
│   ├── agent_client.py     # Agent Zero HTTP + WebSocket client
│   ├── media.py            # Media extraction & sending
│   └── handlers.py         # Telegram handlers
├── docs/
│   ├── architecture.md
│   ├── capabilities.md
│   ├── local-development.md
│   ├── streaming-integration.md
│   └── technical-details.md
├── scripts/
│   └── release-dockerhub.sh
├── Dockerfile
├── docker-compose.yml
├── example-docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

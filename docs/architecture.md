# Architecture

## Overview

The Telegram proxy is a lightweight Python service that bridges Telegram's Bot API with Agent Zero's REST + WebSocket APIs. It **streams responses by default** using Agent Zero's Socket.IO WebSocket. It runs as a Docker container alongside Agent Zero on the same Docker network and supports text, photos, documents, and voice messages in both directions.

```
                    Internet                          Docker Network (proxy-net)
                       |                                       |
  Telegram User ──► Telegram Bot API ──► telegram-proxy ──WebSocket──► agent-zero:80
                                          (long-polling)     (streaming via /state_sync)
```

## How It Works

1. The bot connects to Telegram using **long polling** — it continuously asks Telegram's servers for new messages. No public URL or HTTPS certificate is needed.

2. When a user sends a text message, photo, document, or voice message, the bot forwards it to Agent Zero:
   - **Streaming path (default)**: Ensures the conversation exists via `POST /chat_create`, then queues the message via `POST /message_queue_add` and triggers processing via `POST /message_queue_send`. The proxy connects to Agent Zero's Socket.IO WebSocket (`/state_sync` namespace) to receive the response in real-time as it's generated. This is always attempted first.
   - **Blocking path (automatic fallback)**: If the WebSocket connection fails (e.g., wrong credentials), falls back to `POST /api_message` which returns the full response in one shot.

3. Media attachments (photos, documents, voice messages) are base64-encoded as data URIs and sent via the `attachments` field in `/message_queue_add`. Each attachment has the format `{"path": "data:<mime>;base64,<data>", "name": "<filename>"}`.

4. Agent Zero's response is converted from Markdown to Telegram-compatible HTML using a built-in converter (`md_to_html.py`) and sent back to Telegram. If the response contains markdown media references (`![alt](url)` or `[text](url)` pointing to image/document/audio files), those are extracted, downloaded from Agent Zero, and sent as native Telegram photos, documents, voice messages, or audio files.

5. Each Telegram chat gets its own Agent Zero conversation, tracked by a `context_id` derived from the Telegram chat ID (format: `telegram-<chat_id>`).

## Module Structure

| Module | Purpose |
|---|---|
| `src/bot.py` | Entrypoint — registers Telegram handlers and starts long-polling |
| `src/config.py` | Loads environment variables and exports `is_allowed()`, `context_id_for()` |
| `src/handlers.py` | Command and message handlers — routes to streaming, manages media uploads |
| `src/agent_client.py` | `AgentZeroClient` — HTTP + Socket.IO WebSocket communication with Agent Zero |
| `src/telegram_send.py` | HTML message sending with safe chunking (4096-char limit) and fallback to plain text |
| `src/md_to_html.py` | Native Markdown-to-Telegram-HTML converter (no third-party deps) |
| `src/media.py` | Extracts media references from responses, downloads and sends as native Telegram media |

## Network Flow

```
┌──────────────────────────────────────────────────────────┐
│  Docker Compose                                          │
│                                                          │
│  ┌─────────────────┐       ┌─────────────────────────┐   │
│  │ telegram-proxy   │──────►│ agent-zero              │   │
│  │                  │ HTTP  │                         │   │
│  │ Python bot       │  +    │ Port 80 (internal)      │   │
│  │ (no exposed      │ WS    │ Port 50080 (host)       │   │
│  │  ports)          │       │                         │   │
│  └─────────────────┘       └─────────────────────────┘   │
│         │                                                │
│         │ proxy-net (bridge)                              │
└─────────┼────────────────────────────────────────────────┘
          │
          ▼
   Telegram Bot API
   (api.telegram.org)
```

- **telegram-proxy** does not expose any ports to the host. It only makes outbound connections: to Telegram's API (internet) and to Agent Zero (Docker network via HTTP + WebSocket).
- **agent-zero** exposes port 50080 for the web UI, but the proxy communicates with it internally on port 80.
- **Streaming** uses the same WebSocket protocol as Agent Zero's own web UI (`/state_sync` namespace). See the [Streaming Integration Guide](streaming-integration.md) for the full protocol specification.

## Conversation Mapping

Each Telegram chat ID maps to a unique Agent Zero `context_id`:

| Telegram Chat ID | Agent Zero context_id |
|---|---|
| `123456789` | `telegram-123456789` |
| `987654321` | `telegram-987654321` |

This means:
- Each user (or group chat) gets an independent conversation with the agent
- Conversations persist across messages until explicitly reset with `/reset`
- The `/reset` command calls Agent Zero's `POST /api_reset_chat` to clear the conversation state

## Authentication

Two layers of authentication protect the system:

1. **Telegram → Proxy**: An allowlist of Telegram user IDs. Only users whose IDs are in `ALLOWED_TELEGRAM_USER_IDS` can interact with the bot. If the allowlist is empty, all users are permitted.

2. **Proxy → Agent Zero**: The `X-API-KEY` header and `X-CSRF-Token` header on HTTP requests (`/chat_create`, `/message_queue_add`, `/message_queue_send`), plus session cookies and a CSRF token for WebSocket connections. The API key is found in Agent Zero's Settings > External Services.

# Technical Details

## Docker Image

- **Base image**: `python:3.12-slim`
- **Published to**: `opendigitalsociety/agent-zero-telegram-proxy` on Docker Hub
- **Dependencies**: `python-telegram-bot ~=22.6`, `httpx ~=0.28`, `python-socketio[asyncio_client] ~=5.12`
- **Entrypoint**: `python bot.py`
- **Exposed ports**: None (outbound connections only)

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `AGENT_ZERO_API_KEY` | Yes | — | API key from Agent Zero Settings > External Services |
| `AGENT_ZERO_URL` | No | `http://agent-zero:80` | Agent Zero API base URL |
| `AGENT_ZERO_LOGIN` | No | `admin` | Agent Zero web UI username (for WebSocket auth) |
| `AGENT_ZERO_PASSWORD` | No | (empty) | Agent Zero web UI password (for WebSocket auth) |
| `ALLOWED_TELEGRAM_USER_IDS` | No | (empty = allow all) | Comma-separated Telegram user IDs |
| `REQUEST_TIMEOUT_SECONDS` | No | `120` | Timeout for Agent Zero API calls (seconds) |
| `DRAFT_THROTTLE_MS` | No | `200` | Minimum interval between `sendMessageDraft` updates (ms) |

## Authentication

### Agent Zero API Key

The proxy authenticates with Agent Zero using the `X-API-KEY` HTTP header. To get your API key:

1. Open the Agent Zero web UI
2. Go to **Settings > External Services**
3. Copy the API key/token shown there

The key is auto-generated from your Agent Zero `AUTH_LOGIN` and `AUTH_PASSWORD`.

### Agent Zero WebSocket Auth

For streaming, the bot connects to Agent Zero's Socket.IO WebSocket. This requires:
1. Session cookies from the Agent Zero login endpoint
2. A CSRF token from `GET /csrf_token`

Set `AGENT_ZERO_LOGIN` and `AGENT_ZERO_PASSWORD` to your Agent Zero credentials (the same ones you use for the web UI). If these are not set or login fails, the bot falls back to the blocking API.

### Telegram User Allowlist

Set `ALLOWED_TELEGRAM_USER_IDS` to a comma-separated list of numeric Telegram user IDs:

```
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot) on Telegram.

If this variable is empty or unset, all users are allowed (not recommended for public bots).

## WebSocket Streaming Architecture

The bot uses Agent Zero's Socket.IO WebSocket (the same protocol the web UI uses) to receive real-time response updates:

1. **Connect** to the `/state_sync` namespace with session cookies + CSRF token
2. **Subscribe** by emitting `state_request` with the conversation's `context_id`
3. **Receive** `state_push` events containing a `SnapshotV1` with:
   - `logs[]` — array of log items; items with `type == "response"` contain the agent's reply in their `content` field
   - `log_progress_active` — `true` while the agent is still generating, `false` when done
4. **Forward** each text update to Telegram via `sendMessageDraft` (private) or `editMessageText` (group)
5. **Finalize** with `sendMessage` when the agent is done

If the WebSocket connection fails at any point, the bot transparently falls back to the blocking `POST /api_message` endpoint.

## Timeout Configuration

Agent Zero can take a while to process complex requests. The default timeout is 120 seconds. For long-running agent tasks, increase it:

```
REQUEST_TIMEOUT_SECONDS=300
```

If a request times out, the user sees a friendly error message and can retry.

## Draft Throttle Configuration

The `DRAFT_THROTTLE_MS` setting controls how often the bot sends draft updates to Telegram. Lower values = smoother streaming but more API calls. The default of 200ms provides a good balance.

For group chats, edits are always throttled to ~1 per second regardless of this setting (Telegram's rate limit for `editMessageText`).

## Message Size Handling

Telegram has a 4096-character limit per message. If Agent Zero returns a longer response, the bot automatically splits it into multiple sequential messages. During streaming, only the first 4096 characters are shown in the draft/preview.

## Release Process

To publish a new version to Docker Hub:

```bash
./scripts/release-dockerhub.sh
```

The script:
1. Queries Docker Hub for the latest published version
2. Increments the patch number (e.g., `0.0.1` → `0.0.2`)
3. Builds the Docker image with both version and `latest` tags
4. Pushes both tags to Docker Hub

You must be logged in to Docker Hub (`docker login`) with push access to the `opendigitalsociety` organization.

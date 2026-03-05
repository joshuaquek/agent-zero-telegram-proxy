# Agent Zero Telegram Proxy

Chat with [Agent Zero](https://github.com/agent0ai/agent-zero) through Telegram with **real-time message streaming**. As Agent Zero generates its response, you see it appear live in Telegram — just like watching someone type. Also supports photos, documents, and voice messages in both directions.

```
You (Telegram) ──► Telegram Bot ──► This Proxy ──WebSocket──► Agent Zero
                                         ▲                        │
                                         └── streams chunks ◄─────┘
```

## Streaming by Default

This proxy **streams responses by default** using Agent Zero's Socket.IO WebSocket (`/state_sync` namespace). There is no flag to turn streaming on — it is always the primary path. The proxy only falls back to the blocking API if the WebSocket connection fails (e.g., wrong credentials).

**How it looks in Telegram:**
- **Private chats** — Uses Telegram's `sendMessageDraft` API. Text appears smoothly in a draft bubble as it's generated.
- **Group chats** — Sends a message and edits it progressively as more text arrives.

For full details on the streaming protocol and how to integrate programmatically, see [Streaming Integration Guide](docs/streaming-integration.md).

## Quick Start

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and follow the prompts. Copy the bot token it gives you.

### 2. Get your Agent Zero credentials

Open Agent Zero's web UI (usually `http://localhost:50080`):
- Go to **Settings > External Services** and copy the **API key**.
- Note your **login username and password** — the proxy needs these for streaming via WebSocket.

### 3. Find your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) on Telegram — it replies with your numeric user ID.

### 4. Add to your Docker Compose

Add the `telegram-proxy` service to your existing `docker-compose.yml` / `compose.yml`:

```yaml
  telegram-proxy:
    image: opendigitalsociety/agent-zero-telegram-proxy:latest
    container_name: telegram-proxy
    restart: unless-stopped
    environment:
      - TELEGRAM_BOT_TOKEN=your-bot-token
      - AGENT_ZERO_API_KEY=your-api-key
      - AGENT_ZERO_URL=http://agent-zero:80

      # Streaming credentials — must match AUTH_LOGIN / AUTH_PASSWORD on agent-zero
      - AGENT_ZERO_LOGIN=admin
      - AGENT_ZERO_PASSWORD=your-password

      - ALLOWED_TELEGRAM_USER_IDS=your-user-id
    depends_on:
      - agent-zero
    networks:
      - proxy-net    # same network as your agent-zero service
```

> **Streaming requires `AGENT_ZERO_LOGIN` and `AGENT_ZERO_PASSWORD`** to match your Agent Zero web UI credentials. Without these, the proxy falls back to the blocking API (no real-time streaming).

> See [example-docker-compose.yml](example-docker-compose.yml) for a complete working example.

### 5. Start it up

```bash
docker compose up -d telegram-proxy
```

Now message your bot on Telegram — you'll see Agent Zero's response **streaming in real-time**. You can also send photos, documents, and voice messages.

## How Streaming Works

```
1. User sends message to Telegram bot
2. Proxy queues message via POST /message_queue_add
3. Proxy triggers processing via POST /message_queue_send
4. Proxy connects to Agent Zero's WebSocket (/state_sync namespace)
5. Agent Zero pushes state_push events as it generates text
6. Proxy forwards each chunk to Telegram in real-time
7. When agent finishes, proxy sends the final formatted message
```

The proxy authenticates the WebSocket connection using session cookies from `POST /login` and a CSRF token from `GET /csrf_token`. This is why `AGENT_ZERO_LOGIN` and `AGENT_ZERO_PASSWORD` are needed.

If the WebSocket connection fails for any reason, the proxy transparently falls back to the blocking `POST /api_message` endpoint — the user simply waits for the complete response instead of seeing it stream.

See the [Streaming Integration Guide](docs/streaming-integration.md) for the full API reference, data formats, and code examples.

## Bot Commands

| Command  | What it does |
|----------|-------------|
| `/start` | Shows a welcome message |
| `/reset` | Clears conversation history and starts fresh |

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `AGENT_ZERO_API_KEY` | Yes | — | API key from Agent Zero Settings > External Services |
| `AGENT_ZERO_URL` | No | `http://agent-zero:80` | Agent Zero API base URL |
| `AGENT_ZERO_LOGIN` | **Yes for streaming** | `admin` | Agent Zero web UI username (for WebSocket auth) |
| `AGENT_ZERO_PASSWORD` | **Yes for streaming** | (empty) | Agent Zero web UI password (for WebSocket auth) |
| `ALLOWED_TELEGRAM_USER_IDS` | No | (allow all) | Comma-separated Telegram user IDs |
| `REQUEST_TIMEOUT_SECONDS` | No | `120` | Timeout for Agent Zero API calls (seconds) |
| `DRAFT_THROTTLE_MS` | No | `200` | Minimum interval between draft updates (ms) |

## Documentation

| Document | What's inside |
|----------|---------------|
| [Streaming Integration Guide](docs/streaming-integration.md) | Full API reference for streaming — endpoints, WebSocket protocol, data formats, code examples |
| [Architecture](docs/architecture.md) | How the services connect, network diagram, conversation mapping |
| [Capabilities](docs/capabilities.md) | What the bot can do, API endpoints used, current limitations |
| [Technical Details](docs/technical-details.md) | Environment variables, authentication, release process |
| [Local Development](docs/local-development.md) | Setup guide, running without Docker, troubleshooting |

## Common Commands

| Command | Description |
|---------|-------------|
| `docker compose up -d` | Start all services |
| `docker compose down` | Stop all services |
| `docker compose logs -f telegram-proxy` | Follow the proxy logs |
| `docker compose restart telegram-proxy` | Restart after config changes |

## Links

- [Agent Zero](https://github.com/agent0ai/agent-zero) — The AI agent framework
- [Docker Hub Image](https://hub.docker.com/r/opendigitalsociety/agent-zero-telegram-proxy) — Pre-built Docker image
- [python-telegram-bot](https://python-telegram-bot.org/) — Telegram bot library used

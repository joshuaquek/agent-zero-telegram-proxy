# Agent Zero Telegram Proxy

Chat with [Agent Zero](https://github.com/agent0ai/agent-zero) through Telegram. This runs as a Docker container alongside your Agent Zero setup.

```
You (Telegram) ──► Telegram Bot ──► This Proxy ──► Agent Zero
```

## Quick Start

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and follow the prompts. Copy the bot token it gives you.

### 2. Get your Agent Zero API key

Open Agent Zero's web UI (usually `http://localhost:50080`), go to **Settings > External Services**, and copy the API key.

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
      - ALLOWED_TELEGRAM_USER_IDS=your-user-id
    depends_on:
      - agent-zero
    networks:
      - proxy-net    # same network as your agent-zero service
```

> See [example-docker-compose.yml](example-docker-compose.yml) for a complete working example.

### 5. Start it up

```bash
docker compose up -d telegram-proxy
```

Now message your bot on Telegram — it will respond with Agent Zero's replies.

## Bot Commands

| Command  | What it does |
|----------|-------------|
| `/start` | Shows a welcome message |
| `/reset` | Clears conversation history and starts fresh |

## Documentation

| Document | What's inside |
|----------|---------------|
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

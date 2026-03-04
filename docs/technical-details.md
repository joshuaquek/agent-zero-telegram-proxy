# Technical Details

## Docker Image

- **Base image**: `python:3.12-slim`
- **Published to**: `opendigitalsociety/agent-zero-telegram-proxy` on Docker Hub
- **Dependencies**: `python-telegram-bot ~=21.10`, `httpx ~=0.28`
- **Entrypoint**: `python bot.py`
- **Exposed ports**: None (outbound connections only)

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `AGENT_ZERO_API_KEY` | Yes | — | API key from Agent Zero Settings > External Services |
| `AGENT_ZERO_URL` | No | `http://agent-zero:80` | Agent Zero API base URL |
| `ALLOWED_TELEGRAM_USER_IDS` | No | (empty = allow all) | Comma-separated Telegram user IDs |
| `REQUEST_TIMEOUT_SECONDS` | No | `120` | Timeout for Agent Zero API calls (seconds) |

## Authentication

### Agent Zero API Key

The proxy authenticates with Agent Zero using the `X-API-KEY` HTTP header. To get your API key:

1. Open the Agent Zero web UI
2. Go to **Settings > External Services**
3. Copy the API key/token shown there

The key is auto-generated from your Agent Zero `AUTH_LOGIN` and `AUTH_PASSWORD`.

### Telegram User Allowlist

Set `ALLOWED_TELEGRAM_USER_IDS` to a comma-separated list of numeric Telegram user IDs:

```
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot) on Telegram.

If this variable is empty or unset, all users are allowed (not recommended for public bots).

## Timeout Configuration

Agent Zero can take a while to process complex requests. The default timeout is 120 seconds. For long-running agent tasks, increase it:

```
REQUEST_TIMEOUT_SECONDS=300
```

If a request times out, the user sees a friendly error message and can retry.

## Message Size Handling

Telegram has a 4096-character limit per message. If Agent Zero returns a longer response, the bot automatically splits it into multiple sequential messages.

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

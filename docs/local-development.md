# Local Development

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- A Telegram account
- An Agent Zero instance (either local or via the included docker-compose)

## Getting a Telegram Bot Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Follow the prompts to name your bot
4. BotFather will give you a token like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`
5. Save this token — you'll need it for `TELEGRAM_BOT_TOKEN`

## Getting Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with your user ID (a number like `123456789`)
3. Use this for the `ALLOWED_TELEGRAM_USER_IDS` variable

## Getting the Agent Zero API Key

1. Start Agent Zero and open the web UI (default: `http://localhost:50080`)
2. Log in with your credentials
3. Go to **Settings > External Services**
4. Copy the API key/token

## Running with Docker Compose (Build from Source)

This is the simplest way to run everything locally:

```bash
# 1. Clone the repo
git clone <repo-url>
cd agent-zero-telegram-proxy

# 2. Create your .env file
cp .env.example .env
# Edit .env and fill in your values

# 3. Start both services
docker compose up -d

# 4. Check logs
docker compose logs -f telegram-proxy
```

## Running Without Docker (Python Directly)

Useful for rapid development and debugging:

```bash
# 1. Create a virtual environment
python3.12 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables
export TELEGRAM_BOT_TOKEN="your-token-here"
export AGENT_ZERO_API_KEY="your-api-key-here"
export AGENT_ZERO_URL="http://localhost:50080"  # Agent Zero must be accessible
export ALLOWED_TELEGRAM_USER_IDS="your-user-id"

# 4. Run
python src/bot.py
```

Note: When running outside Docker, `AGENT_ZERO_URL` should point to `http://localhost:50080` (or wherever Agent Zero is accessible from your machine), not `http://agent-zero:80` (which only works inside the Docker network).

## Common Commands

| Command | Description |
|---------|-------------|
| `docker compose up -d` | Start all services |
| `docker compose down` | Stop all services |
| `docker compose logs -f telegram-proxy` | Follow proxy logs |
| `docker compose build telegram-proxy` | Rebuild after code changes |
| `docker compose up -d --build telegram-proxy` | Rebuild and restart |

## Troubleshooting

### Bot doesn't respond to messages

1. Check the logs: `docker compose logs -f telegram-proxy`
2. Verify `TELEGRAM_BOT_TOKEN` is correct
3. Make sure your Telegram user ID is in `ALLOWED_TELEGRAM_USER_IDS`
4. Confirm Agent Zero is running: `docker compose ps`

### "Cannot reach Agent Zero" error

1. Make sure the `agent-zero` container is running: `docker compose ps`
2. Check that `AGENT_ZERO_URL` is correct (`http://agent-zero:80` inside Docker)
3. Verify Agent Zero's web UI is accessible at `http://localhost:50080`

### "Agent Zero took too long to respond"

Agent Zero may need more time for complex tasks. Increase the timeout:

```
REQUEST_TIMEOUT_SECONDS=300
```

### "Sorry, you are not authorized"

Your Telegram user ID is not in the allowlist. Add it to `ALLOWED_TELEGRAM_USER_IDS` in your `.env` file, then restart the proxy:

```bash
docker compose restart telegram-proxy
```

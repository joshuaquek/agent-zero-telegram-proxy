# Capabilities

## What the Bot Can Do

### Message Forwarding
Every text message you send to the Telegram bot is forwarded to Agent Zero. The agent's full response is sent back to you in Telegram. Long responses (over 4096 characters) are automatically split into multiple messages.

### Persistent Conversations
Each Telegram chat maintains its own ongoing conversation with Agent Zero. You can have a multi-turn dialogue just like in the Agent Zero web UI — the agent remembers previous messages in the same chat.

### Conversation Reset
Send `/reset` to clear the conversation history and start fresh. This calls Agent Zero's `POST /api_reset_chat` endpoint.

### Access Control
Restrict bot access to specific Telegram users via an allowlist of user IDs. Unauthorized users receive a rejection message and cannot interact with the agent.

### Typing Indicator
While Agent Zero is processing your message, the bot shows a "typing..." indicator in Telegram so you know it's working.

## Agent Zero API Endpoints Used

| Endpoint | Purpose | When Used |
|---|---|---|
| `POST /api_message` | Send a message and get a response | Every text message |
| `POST /api_reset_chat` | Reset conversation state | `/reset` command |

### `POST /api_message` Request

```json
{
  "message": "Hello, what can you do?",
  "context_id": "telegram-123456789"
}
```

Header: `X-API-KEY: <your-api-key>`

### `POST /api_message` Response

```json
{
  "response": "I can help you with many tasks including..."
}
```

## Limitations

- **Text only**: The bot currently forwards text messages only. Images, documents, voice messages, and other media types are not yet supported.
- **No streaming**: Agent Zero's response is sent as a single message after the agent finishes processing. There is no real-time streaming of partial responses.
- **In-memory state**: The Telegram-to-context mapping is stored in memory. If the proxy container restarts, the mapping resets — but this is usually fine since the context IDs are deterministic (based on chat ID).

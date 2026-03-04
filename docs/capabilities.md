# Capabilities

## What the Bot Can Do

### Real-Time Streaming
The bot streams Agent Zero's response to you in real-time as it's being generated. In **private chats**, it uses Telegram's native `sendMessageDraft` API — you see text appearing smoothly in a draft bubble, just like watching someone type. In **group chats**, it sends a message and edits it progressively as more text arrives. When the agent finishes, the final message replaces the draft/preview.

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

## How Streaming Works

### Private Chats (sendMessageDraft)

```
User sends message
  → Bot queues message to Agent Zero
  → Bot subscribes to Agent Zero's WebSocket for state updates
  → As the agent generates text, each chunk triggers a sendMessageDraft call
  → Telegram natively animates the growing text in a draft bubble
  → When the agent finishes, bot sends the final message (draft disappears)
```

- Uses Telegram Bot API 9.5 `sendMessageDraft` — purpose-built for streaming
- Draft updates are throttled to avoid rate limits (default: 200ms between updates)
- No markdown formatting during streaming (to avoid parse errors on partial text); formatting applies on the final message

### Group Chats (sendMessage + editMessageText)

```
User sends message
  → Bot queues message to Agent Zero
  → Bot subscribes to Agent Zero's WebSocket for state updates
  → First chunk: bot sends a new message
  → Subsequent chunks: bot edits that message with the growing text
  → When the agent finishes, bot does a final edit with the complete response
```

- Edits are throttled to ~1 per second (Telegram's rate limit for message edits)
- `sendMessageDraft` is not available in group chats

### Fallback (No WebSocket)

If the WebSocket connection to Agent Zero fails, the bot automatically falls back to the blocking `POST /api_message` endpoint — the same behavior as before streaming was added. The user simply waits for the full response instead of seeing it stream.

## Agent Zero API Endpoints Used

| Endpoint | Purpose | When Used |
|---|---|---|
| `POST /message_queue_add` | Queue a message for the agent | Every text message (streaming path) |
| `POST /message_queue_send` | Trigger agent processing | Every text message (streaming path) |
| `POST /api_message` | Send a message, get full response (blocking) | Fallback when WebSocket unavailable |
| `POST /api_reset_chat` | Reset conversation state | `/reset` command |
| WebSocket `/state_sync` | Real-time state push subscription | Streaming response updates |

## Limitations

- **Text only**: The bot currently forwards text messages only. Images, documents, voice messages, and other media types are not yet supported.
- **No markdown in drafts**: During streaming, draft text is sent as plain text. Markdown formatting is only applied on the final sent message.
- **In-memory state**: The Telegram-to-context mapping is stored in memory. If the proxy container restarts, the mapping resets — but this is usually fine since the context IDs are deterministic (based on chat ID).

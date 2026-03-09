# Capabilities

## What the Bot Can Do

### Real-Time Streaming (Default)
Streaming is the **default and primary** communication path — there is no flag to enable it. The bot always attempts to stream Agent Zero's response in real-time using the WebSocket `/state_sync` namespace. It only falls back to the blocking API if the WebSocket connection fails.

In **private chats**, it uses Telegram's native `sendMessageDraft` API — you see text appearing smoothly in a draft bubble, just like watching someone type. In **group chats**, it sends a message and edits it progressively as more text arrives. When the agent finishes, the final message replaces the draft/preview.

For the full streaming protocol, API reference, and integration details, see the [Streaming Integration Guide](streaming-integration.md).

### Message Forwarding
Every text message you send to the Telegram bot is forwarded to Agent Zero. The agent's full response is sent back to you in Telegram. Long responses (over 4096 characters) are automatically split into multiple messages.

### Media Support (Images, Documents, Voice)
You can send **photos**, **documents** (PDF, Word, Excel, ZIP, etc.), and **voice messages** to the bot — they are forwarded to Agent Zero as base64 attachments along with any caption text. When Agent Zero's response contains markdown media references, the bot downloads them and sends them as native Telegram media:

- `![alt](url)` with image extensions → sent as a **photo**
- `![alt](url)` or `[text](url)` with document extensions (.pdf, .docx, .csv, etc.) → sent as a **document**
- `![alt](url)` or `[text](url)` with voice/audio extensions (.ogg, .mp3, etc.) → sent as **voice** or **audio**

### Persistent Conversations
Each Telegram chat maintains its own ongoing conversation with Agent Zero. You can have a multi-turn dialogue just like in the Agent Zero web UI — the agent remembers previous messages in the same chat.

### Conversation Reset
Send `/reset` to clear the conversation history and start fresh. This calls Agent Zero's `POST /api_reset_chat` endpoint.

### Access Control
Restrict bot access to specific Telegram users via an allowlist of user IDs. Unauthorized users receive a rejection message and cannot interact with the agent.

### Typing Indicator
While Agent Zero is processing your message, the bot shows a "typing..." indicator in Telegram so you know it's working.

## How Streaming Works (Default Behavior)

Streaming is always attempted first. See the [Streaming Integration Guide](streaming-integration.md) for the full protocol specification.

### Private Chats (sendMessageDraft)

```
User sends message
  → Bot queues message via POST /message_queue_add + /message_queue_send
  → Bot connects to Agent Zero's WebSocket (/state_sync namespace)
  → As the agent generates text, each state_push triggers a sendMessageDraft call
  → Telegram natively animates the growing text in a draft bubble
  → When the agent finishes, bot sends the final message (draft disappears)
```

- Uses Telegram Bot API 9.5 `sendMessageDraft` — purpose-built for streaming
- Draft updates are throttled to avoid rate limits (default: 200ms between updates)
- During streaming, drafts are converted to Telegram HTML when the markup has balanced tags (via `safe_md_to_tg_html`); if tags are unbalanced mid-stream, the draft falls back to plain text. Full formatting always applies on the final message.

### Group Chats (sendMessage + editMessageText)

```
User sends message
  → Bot queues message via POST /message_queue_add + /message_queue_send
  → Bot connects to Agent Zero's WebSocket (/state_sync namespace)
  → First chunk: bot sends a new message
  → Subsequent chunks: bot edits that message with the growing text
  → When the agent finishes, bot does a final edit with the complete response
```

- Edits are throttled to ~1 per second (Telegram's rate limit for message edits)
- `sendMessageDraft` is not available in group chats
- If the final response contains media (images, documents, audio), the streaming preview message is deleted and replaced with native Telegram media messages

### Fallback (No WebSocket)

If the WebSocket connection to Agent Zero fails (typically due to missing or incorrect `AGENT_ZERO_LOGIN` / `AGENT_ZERO_PASSWORD`), the bot automatically falls back to the blocking `POST /api_message` endpoint. The user simply waits for the full response instead of seeing it stream. To ensure streaming works, verify that these credentials match your Agent Zero instance's `AUTH_LOGIN` and `AUTH_PASSWORD`.

## Agent Zero API Endpoints Used

| Endpoint | Purpose | When Used |
|---|---|---|
| `POST /login` | Authenticate and get session cookies | Every streaming request |
| `GET /csrf_token` | Fetch CSRF token for WebSocket and HTTP requests | Every streaming request |
| `POST /chat_create` | Ensure conversation context exists | Every message (streaming path) |
| `POST /message_queue_add` | Queue a message (with optional attachments) for the agent | Every message (streaming path) |
| `POST /message_queue_send` | Trigger agent processing of queued messages | Every message (streaming path) |
| `POST /api_message` | Send a message, get full response (blocking) | Fallback when WebSocket unavailable |
| `POST /api_reset_chat` | Reset conversation state | `/reset` command |
| WebSocket `/state_sync` | Real-time state push subscription | Streaming response updates |

## Limitations

- **Supported media types**: Photos, documents, and voice messages work in both directions. Video and sticker support is not yet implemented.
- **Partial formatting in drafts**: During streaming, drafts are converted to Telegram HTML only when all tags are balanced. Incomplete markdown (e.g., an unclosed bold `**`) falls back to plain text for that draft update. Full formatting always applies on the final message.
- **Stateless proxy**: The context mapping is deterministic (`telegram-<chat_id>` or `telegram-<chat_id>-topic-<thread_id>`), so restarting the proxy container loses no state — conversations resume seamlessly.

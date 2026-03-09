# Streaming Integration Guide

This proxy streams Agent Zero's responses to Telegram in real-time by default. This document explains the full streaming protocol so that agents, developers, or other services can understand and integrate with the same mechanism.

## Overview

Streaming is the **default and primary** communication path. The proxy uses Agent Zero's Socket.IO WebSocket on the `/state_sync` namespace to receive response chunks as the agent generates them. There is no flag to enable streaming — it is always attempted first. The blocking API (`POST /api_message`) is only used as an automatic fallback if the WebSocket connection fails.

```
┌──────────────────────────────────────────────────────────────────┐
│  DEFAULT: Streaming Path (WebSocket)                             │
│                                                                  │
│  1. POST /message_queue_add     ← queue the user's message      │
│  2. POST /message_queue_send    ← trigger agent processing      │
│  3. WebSocket /state_sync       ← receive chunks in real-time   │
│                                                                  │
│  FALLBACK: Blocking Path (only if WebSocket fails)               │
│                                                                  │
│  1. POST /api_message           ← send message, wait for full   │
│                                    response                      │
└──────────────────────────────────────────────────────────────────┘
```

## Prerequisites for Streaming

Streaming requires WebSocket authentication using Agent Zero's web UI credentials:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AGENT_ZERO_LOGIN` | **Yes** | `admin` | Agent Zero web UI username |
| `AGENT_ZERO_PASSWORD` | **Yes** | (empty) | Agent Zero web UI password |

These must match the `AUTH_LOGIN` and `AUTH_PASSWORD` configured on your Agent Zero instance. Without valid credentials, the WebSocket connection fails and the proxy falls back to the blocking API.

**In Docker Compose**, ensure the credentials match:

```yaml
services:
  agent-zero:
    environment:
      - AUTH_LOGIN=admin
      - AUTH_PASSWORD=YourPassword123

  telegram-proxy:
    environment:
      # These MUST match the values above
      - AGENT_ZERO_LOGIN=admin
      - AGENT_ZERO_PASSWORD=YourPassword123
```

## Streaming Protocol Step by Step

### Step 1: Authenticate and Get Session Cookies

```
POST {AGENT_ZERO_URL}/login
Content-Type: application/x-www-form-urlencoded

username=admin&password=YourPassword123
```

This returns session cookies needed for the WebSocket handshake.

### Step 2: Fetch CSRF Token

```
GET {AGENT_ZERO_URL}/csrf_token
Cookie: <session cookies from step 1>

Response: {"token": "abc123..."}
```

The CSRF token is passed during the WebSocket connection as part of the `auth` payload.

### Step 3: Connect to the WebSocket

Connect a Socket.IO client to the `/state_sync` namespace:

```
URL:       {AGENT_ZERO_URL}
Namespace: /state_sync
Headers:   Cookie: <session cookies from step 1>
Auth:      {"csrf_token": "abc123..."}
```

### Step 4: Subscribe to State Updates

Emit a `state_request` event on the `/state_sync` namespace:

```json
{
  "context": "telegram-<chat_id>",
  "log_from": 0,
  "notifications_from": 0,
  "timezone": "UTC"
}
```

The `context` field identifies the conversation. This proxy uses the format `telegram-<chat_id>` (e.g., `telegram-123456789`). For forum topics, the format is `telegram-<chat_id>-topic-<thread_id>` (e.g., `telegram-987654321-topic-555`).

### Step 5: Ensure Context and Queue the User's Message

First, ensure the conversation context exists:

```
POST {AGENT_ZERO_URL}/chat_create
X-API-KEY: <your-api-key>
X-CSRF-Token: <csrf-token>
Content-Type: application/json

{"new_context": "telegram-<chat_id>"}
```

Then queue the message:

```
POST {AGENT_ZERO_URL}/message_queue_add
X-API-KEY: <your-api-key>
Content-Type: application/json

{
  "context": "telegram-<chat_id>",
  "text": "Hello, agent!",
  "attachments": []
}
```

For messages with media attachments (photos, documents, voice):

```json
{
  "context": "telegram-<chat_id>",
  "text": "What's in this image?",
  "attachments": [
    {
      "path": "data:image/jpeg;base64,<base64-encoded data>",
      "name": "photo.jpg"
    }
  ]
}
```

### Step 6: Trigger Agent Processing

```
POST {AGENT_ZERO_URL}/message_queue_send
X-API-KEY: <your-api-key>
Content-Type: application/json

{
  "context": "telegram-<chat_id>",
  "send_all": true
}
```

### Step 7: Receive Streaming Chunks

After triggering processing, the WebSocket emits `state_push` events as the agent generates its response:

```json
{
  "snapshot": {
    "logs": [
      {
        "type": "response",
        "content": "Here is the agent's response so far..."
      },
      {
        "type": "tool_call",
        "content": "..."
      }
    ],
    "log_progress_active": true
  }
}
```

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `snapshot.logs[]` | array | Array of log items from the agent |
| `snapshot.logs[].type` | string | `"response"` for the agent's text reply, other values for tool calls etc. |
| `snapshot.logs[].content` | string | The text content of that log item |
| `snapshot.log_progress_active` | boolean | `true` while the agent is still generating, `false` when done |

**To extract the agent's response text:** find all log items where `type == "response"` and concatenate their `content` fields (joined by double newlines). The proxy collects all response logs from the new entries (after the baseline) and combines them — this handles multi-part responses from the agent.

**To detect completion:** check when `log_progress_active` becomes `false`.

**Baseline tracking:** The first `state_push` after subscribing contains historical logs from previous turns. Record `len(logs)` as the baseline on the first push and only process `logs[baseline:]` on subsequent pushes. If `len(logs)` drops below the baseline (the server switched to a per-turn view), reset the baseline to 0.

**Image path scanning:** The proxy also scans all new log entries (not just `type == "response"`) for Agent Zero container file paths matching `/a0/usr/<path>.<image_ext>`. Any image paths found in tool outputs, code execution results, etc. that aren't already referenced in the response text are appended to the response. This ensures screenshots and generated images from agent tools are forwarded to Telegram even when the agent doesn't explicitly include them in its text reply.

### Step 8: Forward to Telegram

As each `state_push` arrives with new response text:

- **Private chats**: Call `sendMessageDraft` with the current text (throttled to every 200ms)
- **Group chats**: Call `sendMessage` for the first chunk, then `editMessageText` for subsequent chunks (throttled to every 1 second)

When `log_progress_active` becomes `false`, send the final complete message with full markdown formatting and media extraction.

## Complete Streaming Flow Diagram

```
Telegram User
    │
    ▼
handle_message()
    │
    ▼
POST /login ──► get session cookies
GET /csrf_token ──► get CSRF token
    │
    ▼
WebSocket connect to /state_sync
    │ (with cookies + CSRF)
    ▼
emit "state_request" { context: "telegram-<chat_id>" }
    │
    ▼
POST /message_queue_add ──► queue user's message
POST /message_queue_send ──► trigger agent processing
    │
    ▼
┌─── Listen for "state_push" events ◄──────────────┐
│                                                    │
│  Extract response text from snapshot.logs          │
│  where type == "response"                          │
│                                                    │
│  Forward chunk to Telegram:                        │
│    Private: sendMessageDraft (every 200ms)         │
│    Group:   editMessageText  (every 1s)            │
│                                                    │
│  if log_progress_active == false → done            │
│  else → wait for next state_push ─────────────────┘
│
▼
Send final formatted message with media
```

## Fallback: Blocking API

If the WebSocket connection fails (auth error, network issue, etc.), the proxy automatically falls back to:

```
POST {AGENT_ZERO_URL}/api_message
X-API-KEY: <your-api-key>
Content-Type: application/json

{
  "message": "Hello, agent!",
  "context_id": "telegram-<chat_id>"
}

Response:
{
  "response": "The complete agent response text."
}
```

This returns the full response in one shot — no streaming. The user sees the complete reply after the agent finishes processing.

## Agent Zero API Endpoints Reference

| Endpoint | Method | Auth | Purpose | Used In |
|---|---|---|---|---|
| `/login` | POST | username/password | Get session cookies for WebSocket | Streaming |
| `/csrf_token` | GET | Session cookie | Get CSRF token for WebSocket | Streaming |
| `/chat_create` | POST | X-API-KEY + CSRF | Ensure conversation context exists | Streaming |
| `/message_queue_add` | POST | X-API-KEY + CSRF | Queue a message with optional attachments | Streaming |
| `/message_queue_send` | POST | X-API-KEY + CSRF | Trigger agent processing of queued messages | Streaming |
| `/state_sync` (WebSocket) | Socket.IO | Session cookie + CSRF | Real-time state push subscription | Streaming |
| `/api_message` | POST | X-API-KEY | Send message and get full response (blocking) | Fallback only |
| `/api_reset_chat` | POST | X-API-KEY | Reset conversation state | `/reset` command |

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `AGENT_ZERO_URL` | `http://agent-zero:80` | Agent Zero API base URL |
| `AGENT_ZERO_API_KEY` | (required) | API key for REST endpoint auth |
| `AGENT_ZERO_LOGIN` | `admin` | Username for WebSocket auth (must match Agent Zero's `AUTH_LOGIN`) |
| `AGENT_ZERO_PASSWORD` | (empty) | Password for WebSocket auth (must match Agent Zero's `AUTH_PASSWORD`) |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Max time to wait for agent response |
| `DRAFT_THROTTLE_MS` | `200` | Minimum ms between Telegram draft updates |

## Troubleshooting Streaming

**Streaming not working (responses appear all at once):**
- Check that `AGENT_ZERO_LOGIN` and `AGENT_ZERO_PASSWORD` match your Agent Zero instance's `AUTH_LOGIN` and `AUTH_PASSWORD`
- Check the proxy logs: `docker compose logs -f telegram-proxy`
- Look for `WebSocket connection failed, falling back to blocking API` — this means auth failed

**Slow or choppy streaming:**
- Lower `DRAFT_THROTTLE_MS` (e.g., `100`) for smoother updates in private chats
- Group chats are limited to ~1 edit/second by Telegram's rate limits — this cannot be changed

**Timeout errors:**
- Increase `REQUEST_TIMEOUT_SECONDS` for complex agent tasks (e.g., `300` for 5 minutes)

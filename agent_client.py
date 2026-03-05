"""Agent Zero HTTP + WebSocket streaming client."""

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import socketio

from config import REQUEST_TIMEOUT, logger


@dataclass
class StreamState:
    """Tracks the latest response text and completion status from state_push events."""
    response_text: str = ""
    is_done: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class AgentZeroClient:
    """Communicates with Agent Zero via HTTP (message sending, reset) and
    Socket.IO WebSocket (streaming response via state_push)."""

    def __init__(self, base_url: str, api_key: str, login: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.login = login
        self.password = password

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Create an HTTP client with auth cookies from Agent Zero login."""
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        if self.login and self.password:
            try:
                await client.post(
                    f"{self.base_url}/login",
                    data={"username": self.login, "password": self.password},
                    follow_redirects=True,
                )
            except Exception:
                logger.debug("Login request failed, continuing with API key auth")
        return client

    async def send_message_blocking(self, context_id: str, text: str) -> str:
        """Fallback: send message via blocking /api_message endpoint."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/api_message",
                json={"message": text, "context_id": context_id},
                headers={"X-API-KEY": self.api_key},
            )
            if response.status_code == 404 and context_id:
                logger.info("Context %s not found, retrying without context_id", context_id)
                response = await client.post(
                    f"{self.base_url}/api_message",
                    json={"message": text, "context_id": ""},
                    headers={"X-API-KEY": self.api_key},
                )
            response.raise_for_status()
            data = response.json()
        return data.get("response") or data.get("message") or str(data)

    async def send_message_streaming(self, context_id: str, text: str, attachments: list | None = None):
        """Send a message and yield (response_text, is_done) as the agent streams."""
        http_client = await self._get_http_client()
        stream_state = StreamState()

        sio = socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )

        csrf_token = None
        runtime_id = None
        try:
            resp = await http_client.get(f"{self.base_url}/csrf_token")
            if resp.status_code == 200:
                csrf_data = resp.json()
                csrf_token = csrf_data.get("token") or csrf_data.get("csrf_token")
                runtime_id = csrf_data.get("runtime_id")
        except Exception:
            logger.debug("Could not fetch CSRF token, proceeding without it")

        headers = {"Origin": self.base_url}
        cookie_parts = []
        if http_client.cookies:
            cookie_parts = [f"{k}={v}" for k, v in http_client.cookies.items()]
        if csrf_token and runtime_id:
            cookie_parts.append(f"csrf_token_{runtime_id}={csrf_token}")
        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)

        auth = {}
        if csrf_token:
            auth["csrf_token"] = csrf_token

        @sio.on("state_push", namespace="/state_sync")
        async def on_state_push(data):
            envelope = data if isinstance(data, dict) else {}
            snapshot = envelope.get("snapshot") or envelope.get("data", {}).get("snapshot", {})
            if not snapshot:
                logger.debug("[state_push] No snapshot in envelope")
                return

            logs = snapshot.get("logs", [])
            log_active = snapshot.get("log_progress_active", True)
            logger.info("[state_push] logs=%d, active=%s, types=%s",
                        len(logs), log_active,
                        [l.get("type") for l in logs])
            response_parts = []
            for log_item in logs:
                if log_item.get("type") == "response":
                    content = log_item.get("content", "")
                    if content:
                        response_parts.append(content)
                        logger.info("[state_push] response chunk (%d chars): %s",
                                    len(content), content[:200])

            if response_parts:
                stream_state.response_text = "\n\n".join(response_parts)
                stream_state.event.set()

            if not log_active:
                stream_state.is_done = True
                logger.info("[state_push] Agent done, final text (%d chars)", len(stream_state.response_text))
                stream_state.event.set()

        # Connect to Socket.IO
        try:
            await sio.connect(
                self.base_url,
                namespaces=["/state_sync"],
                headers=headers,
                auth=auth if auth else None,
                wait_timeout=10,
            )
        except Exception:
            logger.warning("WebSocket connection failed, falling back to blocking API")
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True
            return

        # Subscribe to state updates
        try:
            await sio.emit(
                "state_request",
                {
                    "context": context_id,
                    "log_from": 0,
                    "notifications_from": 0,
                    "timezone": "UTC",
                },
                namespace="/state_sync",
            )
        except Exception:
            logger.warning("Failed to send state_request")

        # Queue and send the message via HTTP (web UI path)
        queue_headers = {"X-API-KEY": self.api_key}
        if csrf_token:
            queue_headers["X-CSRF-Token"] = csrf_token
        try:
            resp = await http_client.post(
                f"{self.base_url}/chat_create",
                json={"new_context": context_id},
                headers=queue_headers,
            )
            if resp.status_code == 200:
                logger.debug("Context %s ensured via chat_create", context_id)

            await http_client.post(
                f"{self.base_url}/message_queue_add",
                json={"context": context_id, "text": text, "attachments": attachments or []},
                headers=queue_headers,
            )
            await http_client.post(
                f"{self.base_url}/message_queue_send",
                json={"context": context_id, "send_all": True},
                headers=queue_headers,
            )
        except Exception:
            logger.warning("message_queue path failed, trying /api_message via WebSocket fallback")
            await sio.disconnect()
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True
            return

        # Stream response chunks
        last_text = ""
        timeout_at = time.monotonic() + REQUEST_TIMEOUT
        try:
            while time.monotonic() < timeout_at:
                stream_state.event.clear()
                try:
                    await asyncio.wait_for(stream_state.event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    if not sio.connected:
                        break
                    continue

                current_text = stream_state.response_text
                if current_text != last_text:
                    last_text = current_text
                    yield current_text, stream_state.is_done

                if stream_state.is_done:
                    break

            if not stream_state.is_done and last_text:
                yield last_text, True
        finally:
            try:
                await sio.disconnect()
            except Exception:
                pass
            await http_client.aclose()

    async def reset_chat(self, context_id: str) -> None:
        """Reset a conversation."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            await client.post(
                f"{self.base_url}/api_reset_chat",
                json={"context_id": context_id},
                headers={"X-API-KEY": self.api_key},
            )

"""Agent Zero HTTP + WebSocket streaming client."""

import asyncio
import re
import time
from dataclasses import dataclass, field

import httpx
import socketio

from config import REQUEST_TIMEOUT, logger

# Match /a0/usr/<path>.<image_ext> in any log content (agent thoughts, code_exe, etc.)
_A0_IMAGE_PATH_RE = re.compile(r'/a0/usr/(\S+\.(?:png|jpg|jpeg|gif|bmp|webp))', re.IGNORECASE)


@dataclass
class StreamState:
    """Tracks the latest response text and completion status from state_push events."""
    response_text: str = ""
    status_line: str = ""
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
        self._known_contexts: set[str] = set()

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
        http_client = await self._get_http_client()
        queue_headers = {"X-API-KEY": self.api_key}
        # Fetch CSRF token for queue endpoints
        try:
            resp = await http_client.get(f"{self.base_url}/csrf_token")
            if resp.status_code == 200:
                csrf_data = resp.json()
                csrf_token = csrf_data.get("token") or csrf_data.get("csrf_token")
                if csrf_token:
                    queue_headers["X-CSRF-Token"] = csrf_token
        except Exception:
            pass
        try:
            # Ensure the context exists
            if context_id not in self._known_contexts:
                resp = await http_client.post(
                    f"{self.base_url}/chat_create",
                    json={"new_context": context_id},
                    headers=queue_headers,
                )
                if resp.status_code == 200:
                    self._known_contexts.add(context_id)
            # Use the message queue path (same as streaming) to preserve context mapping
            await http_client.post(
                f"{self.base_url}/message_queue_add",
                json={"context": context_id, "text": text, "attachments": []},
                headers=queue_headers,
            )
            await http_client.post(
                f"{self.base_url}/message_queue_send",
                json={"context": context_id, "send_all": True},
                headers=queue_headers,
            )
            # Poll for completion via context logs
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                try:
                    resp = await http_client.get(
                        f"{self.base_url}/state",
                        params={"context": context_id, "log_from": 0},
                        headers=queue_headers,
                    )
                    if resp.status_code == 200:
                        state = resp.json()
                        snapshot = state.get("snapshot", {})
                        if not snapshot.get("log_progress_active", True):
                            logs = snapshot.get("logs", [])
                            parts = [l.get("content", "") for l in logs if l.get("type") == "response" and l.get("content")]
                            if parts:
                                return "\n\n".join(parts)
                            break
                except Exception:
                    pass
            return "(Agent Zero returned an empty response.)"
        finally:
            await http_client.aclose()

    async def send_message_streaming(self, context_id: str, text: str, attachments: list | None = None):
        """Send a message and yield (response_text, is_done) as the agent streams."""
        t0 = time.monotonic()
        logger.info("[stream:%s] START send_message_streaming", context_id)
        http_client = await self._get_http_client()
        logger.info("[stream:%s] HTTP client ready (+%.2fs)", context_id, time.monotonic() - t0)
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
        # Build cookie header from the jar (avoids CookieConflict on duplicate names)
        if http_client.cookies:
            cookie_parts.append("; ".join(f"{c.name}={c.value}" for c in http_client.cookies.jar))
        if csrf_token and runtime_id:
            cookie_parts.append(f"csrf_token_{runtime_id}={csrf_token}")
        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)

        auth = {}
        if csrf_token:
            auth["csrf_token"] = csrf_token

        # Track how many logs existed before we sent our message so we can
        # ignore historical entries and only process the NEW response.
        baseline_log_count: int | None = None

        @sio.on("state_push", namespace="/state_sync")
        async def on_state_push(data):
            nonlocal baseline_log_count

            envelope = data if isinstance(data, dict) else {}
            snapshot = envelope.get("snapshot") or envelope.get("data", {}).get("snapshot", {})
            if not snapshot:
                logger.debug("[state_push] No snapshot in envelope")
                return

            logs = snapshot.get("logs", [])
            log_active = snapshot.get("log_progress_active", True)

            # First push after subscribing: record how many historical logs
            # exist so we skip them when looking for the current response.
            if baseline_log_count is None:
                baseline_log_count = len(logs)
                logger.info("[state_push] baseline log count set to %d", baseline_log_count)
                # Don't process this initial snapshot — it's history
                if not log_active:
                    # Edge case: agent already idle — no message in flight yet
                    pass
                return

            # If the snapshot now has fewer logs than our baseline, the server
            # switched to a per-turn view — reset baseline so we see all logs.
            if len(logs) < baseline_log_count:
                baseline_log_count = 0

            # Only look at logs AFTER the baseline (i.e. new logs from our message)
            new_logs = logs[baseline_log_count:]
            logger.info("[state_push] total_logs=%d, new=%d, active=%s, new_types=%s",
                        len(logs), len(new_logs), log_active,
                        [l.get("type") for l in new_logs])

            # Log ALL new log items for debugging media/screenshot issues
            for log_item in new_logs:
                log_type = log_item.get("type", "?")
                content = log_item.get("content", "")
                logger.info("[state_push] log type=%s, content_len=%d, content_preview=%r",
                            log_type, len(content), content[:300] if content else "")

            # Extract intermediate status from agent thoughts and code_exe
            for log_item in new_logs:
                log_type = log_item.get("type", "")
                content = log_item.get("content", "")
                if not content:
                    continue
                if log_type == "agent":
                    try:
                        import json as _json
                        parsed = _json.loads(content)
                        thoughts = parsed.get("thoughts", [])
                        if thoughts:
                            thought = thoughts[0][:100]
                            stream_state.status_line = thought
                            stream_state.event.set()
                    except (ValueError, TypeError):
                        pass
                elif log_type == "code_exe":
                    # Take the last non-empty line as the most current status
                    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
                    if lines:
                        stream_state.status_line = f"terminal> {lines[-1][:150]}"
                        stream_state.event.set()

            # Concatenate ALL response content from new logs
            response_parts = []
            for log_item in new_logs:
                if log_item.get("type") == "response":
                    content = log_item.get("content", "")
                    if content:
                        response_parts.append(content)
            latest_response = "\n\n".join(response_parts) if response_parts else ""

            # Scan ALL log types for /a0/usr/ image paths (screenshots etc.)
            # that aren't already referenced in the response text.
            found_images: list[str] = []
            for log_item in new_logs:
                content = log_item.get("content", "")
                if not content:
                    continue
                for m in _A0_IMAGE_PATH_RE.finditer(content):
                    path = "/a0/usr/" + m.group(1)
                    if path not in latest_response and path not in found_images:
                        found_images.append(path)
            if found_images:
                logger.info("[state_push] Found image paths in logs: %s", found_images)
                # Append as plain-text paths so media.py can extract them
                latest_response = latest_response + "\n\n" + "\n".join(found_images) if latest_response else "\n".join(found_images)

            if latest_response:
                logger.info("[state_push] response (%d chars): %s",
                            len(latest_response), latest_response[:200])
                stream_state.response_text = latest_response
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
            logger.info("[stream:%s] WebSocket connected (+%.2fs)", context_id, time.monotonic() - t0)
        except Exception:
            logger.warning("WebSocket connection failed, falling back to blocking API")
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True, ""
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
            logger.info("[stream:%s] state_request sent (+%.2fs)", context_id, time.monotonic() - t0)
        except Exception:
            logger.warning("Failed to send state_request")

        # Queue and send the message via HTTP (web UI path)
        queue_headers = {"X-API-KEY": self.api_key}
        if csrf_token:
            queue_headers["X-CSRF-Token"] = csrf_token
        try:
            # Only create the chat context if it's the first time we've seen it.
            # Calling chat_create on every message creates duplicate chats in Agent Zero.
            if context_id not in self._known_contexts:
                resp = await http_client.post(
                    f"{self.base_url}/chat_create",
                    json={"new_context": context_id},
                    headers=queue_headers,
                )
                if resp.status_code == 200:
                    self._known_contexts.add(context_id)
                    logger.debug("Context %s created via chat_create", context_id)

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
            logger.info("[stream:%s] message queued and sent (+%.2fs)", context_id, time.monotonic() - t0)
        except Exception:
            logger.warning("message_queue path failed, trying /api_message via WebSocket fallback")
            await sio.disconnect()
            await http_client.aclose()
            result = await self.send_message_blocking(context_id, text)
            yield result, True, ""
            return

        # Stream response chunks
        last_text = ""
        last_status = ""
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
                current_status = stream_state.status_line
                if current_text != last_text or current_status != last_status:
                    last_text = current_text
                    last_status = current_status
                    yield current_text, stream_state.is_done, current_status

                if stream_state.is_done:
                    # Re-read in case response was updated while consumer processed the yield
                    final = stream_state.response_text
                    if final != last_text:
                        yield final, True, stream_state.status_line
                    break

            if not stream_state.is_done and last_text:
                yield last_text, True, stream_state.status_line
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

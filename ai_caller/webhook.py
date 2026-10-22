"""Signed tool callback. PRA-38 is built in the lab and is not on origin/main yet.

The real client does not send `endpoint` until that field is on origin/main.
This handler is what the platform will POST once an endpoint is registered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any, Callable

MAX_SKEW_SECONDS = 300
MAX_ARGUMENTS = 20
MAX_ARGUMENT_CHARS = 1000
MAX_RESULT_CHARS = 8000
MAX_ERROR_CHARS = 500
MAX_BODY_BYTES = 64 * 1024
MAX_TOOLS_PER_CALL = 60
BURST = 10
REFILL_SECONDS = 6
REPLAY_ERROR = "This request was already handled."

ToolFn = Callable[[dict[str, Any]], str]
_handlers: dict[str, ToolFn] = {}
_lock = threading.Lock()
_done: dict[str, tuple[bytes, int, dict]] = {}
_counts: dict[str, int] = {}
_buckets: dict[str, tuple[float, float]] = {}


class SignatureError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def reset_callback_state() -> None:
    with _lock:
        _done.clear()
        _counts.clear()
        _buckets.clear()
        _handlers.clear()


def register_tool(name: str, fn: ToolFn) -> None:
    _handlers[name] = fn


def signing_key(secret: str) -> bytes:
    """The key is the secret after whsec_, base64-decoded. The secret is not a raw HMAC string."""
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=False)
    except Exception:
        key = b""
    if not key:
        raise SignatureError("The signature is not valid.")
    return key


def sign(secret: str, webhook_id: str, timestamp: str, raw: bytes) -> str:
    message = f"{webhook_id}.{timestamp}.".encode() + raw
    digest = hmac.new(signing_key(secret), message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def verify(
    secret: str,
    webhook_id: str,
    timestamp: str,
    signature: str,
    raw: bytes,
    *,
    now: float | None = None,
) -> None:
    if not secret or not webhook_id or not timestamp or not signature:
        raise SignatureError("The signature is not valid.")
    try:
        issued = int(timestamp)
    except ValueError:
        raise SignatureError("The signature is not valid.") from None
    current = time.time() if now is None else now
    if abs(current - issued) > MAX_SKEW_SECONDS:
        raise SignatureError("The signature is too old.")
    expected = sign(secret, webhook_id, timestamp, raw)
    matched = False
    for part in signature.split():
        matched = _same(part, expected) or matched
    if not matched:
        raise SignatureError("The signature is not valid.")


def _allow(call_ref: str, now: float) -> str | None:
    """Return an error when this call is over the tool budget. None means allowed."""
    count = _counts.get(call_ref, 0)
    if count >= MAX_TOOLS_PER_CALL:
        return "This call has used its tool limit."
    tokens, updated = _buckets.get(call_ref, (float(BURST), now))
    tokens = min(float(BURST), tokens + max(0.0, now - updated) / REFILL_SECONDS)
    if tokens < 1:
        _buckets[call_ref] = (tokens, now)
        return "slow"
    _buckets[call_ref] = (tokens - 1, now)
    _counts[call_ref] = count + 1
    return None


def _arguments(raw_args: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return None, "Arguments must be scalars."
    cleaned: dict[str, Any] = {}
    for key, value in raw_args.items():
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            cleaned[str(key)] = value
        elif isinstance(value, str):
            if len(value) > MAX_ARGUMENT_CHARS:
                return None, "An argument is too long."
            cleaned[str(key)] = value
        else:
            return None, "Arguments must be scalars."
    if len(cleaned) > MAX_ARGUMENTS:
        return None, "A tool takes at most 20 arguments."
    return cleaned, None


def _bounded(status: int, payload: dict) -> tuple[int, dict]:
    encoded = json.dumps(payload).encode()
    if len(encoded) > MAX_BODY_BYTES:
        return 200, {"error": "The response is too large."}
    if "result" in payload and len(str(payload["result"])) > MAX_RESULT_CHARS:
        return 200, {"error": "The result is too long."}
    if "error" in payload and len(str(payload["error"])) > MAX_ERROR_CHARS:
        payload = {"error": str(payload["error"])[:MAX_ERROR_CHARS]}
    return status, payload


def handle_callback(
    raw: bytes,
    *,
    secret: str,
    webhook_id: str,
    timestamp: str,
    signature: str,
    now: float | None = None,
) -> tuple[int, dict]:
    """Verify a voice.tool.call delivery and answer once per idempotencyKey.

    callRef matches the voice.call.start-agent result on an outbound call.
    An incoming call has no voice.call id yet, so this handler does not look one up.
    """
    if len(raw) > MAX_BODY_BYTES:
        return 400, {"error": "The request is too large."}
    current = time.time() if now is None else now
    try:
        verify(secret, webhook_id, timestamp, signature, raw, now=current)
    except SignatureError as exc:
        status = 401 if str(exc) == "The signature is too old." else 401
        return status, {"error": str(exc)}
    try:
        body = json.loads(raw.decode()) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "The request was not JSON."}
    if not isinstance(body, dict):
        return 400, {"error": "Send a JSON object."}
    key = body.get("idempotencyKey")
    call_ref = body.get("callRef")
    tool = body.get("tool")
    if webhook_id != key:
        return 401, {"error": "The signature is not valid."}
    if not isinstance(key, str) or not key or not isinstance(call_ref, str) or not call_ref:
        return 400, {"error": "The request is missing a call or a key."}
    if not isinstance(tool, str) or not tool:
        return 400, {"error": "The request is missing a tool name."}
    with _lock:
        previous = _done.get(key)
        if previous is not None:
            prev_raw, status, payload = previous
            if prev_raw == raw:
                return status, dict(payload)
            return 400, {"error": REPLAY_ERROR}
        limit = _allow(call_ref, current)
        if limit == "slow":
            return 429, {"error": "Too many tool calls at once."}
        fn = _handlers.get(tool)
        if limit:
            answer = _bounded(200, {"error": limit})
        else:
            arguments, problem = _arguments(body.get("arguments"))
            if problem:
                answer = _bounded(200, {"error": problem})
            elif fn is None:
                answer = _bounded(200, {"result": "Received."})
            else:
                try:
                    answer = _bounded(200, {"result": str(fn(arguments or {}))})
                except Exception:
                    answer = _bounded(200, {"error": "The tool failed."})
        _done[key] = (raw, answer[0], answer[1])
        return answer

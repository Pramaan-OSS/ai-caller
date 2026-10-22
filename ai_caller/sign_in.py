"""Managed sign-in. A local address is not sent to the address list."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from ai_caller.database import call_saved, saved_action_key
from ai_caller.pramaan import PramaanError, startup_notice

LOCAL_SKIP = (
    "Managed sign-in is using a local address, so the address list was left unchanged."
)
UNAVAILABLE = "Managed sign-in is not available yet."
NOT_ALLOWED = "This account is not allowed."
SIGN_IN_REQUIRED = "Sign in to continue."
COOKIE = "ai_caller_session"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def app_origin(port: int) -> str:
    raw = (os.environ.get("APP_URL") or "").strip()
    if raw:
        return raw
    return f"http://localhost:{port}"


def public_origin(url: str) -> str | None:
    """The origin when it is a public https address. Otherwise nothing."""
    parsed = urlparse((url or "").strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path not in ("", "/"):
        return None
    if host.endswith(".") or "*" in host:
        return None
    folded = host.lower()
    if folded in _LOCAL_HOSTS or folded.endswith(".localhost"):
        return None
    if _is_ip(host) or "." not in folded:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{folded}{port}"


def request_origin(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = parsed.hostname
    if parsed.scheme in {"http", "https"} and host:
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    return (url or "").strip().rstrip("/")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class Gate:
    """The sign-in server address and this app's own sessions."""

    def __init__(self, auth_url: str, origin: str) -> None:
        self.auth_url = auth_url.rstrip("/")
        self.origin = request_origin(origin)
        self.callback = self.origin + "/sign-in/callback"
        self.sessions: dict[str, dict] = {}

    def open_session(self, user_id: str, email: str | None) -> str:
        secret = secrets.token_urlsafe(32)
        self.sessions[secret] = {"userId": user_id, "email": email or ""}
        return secret


def open_sign_in(client, *, port: int) -> tuple[Gate | None, str]:
    """Provision only when a key is set. No key leaves the console open."""
    if not os.environ.get("PRAMAAN_API_KEY"):
        return None, ""
    return prepare_sign_in(client, origin=app_origin(port))


def prepare_sign_in(client, *, origin: str, folder: Path | None = None) -> tuple[Gate | None, str]:
    """Provision managed sign-in. A local address does not change the address list."""
    try:
        payload = call_saved(client, "auth.backend.provision", {}, folder=folder)
    except PramaanError as exc:
        return None, startup_notice(exc, UNAVAILABLE)
    auth_url = payload.get("authUrl") if isinstance(payload, dict) else None
    if not isinstance(auth_url, str) or not auth_url.startswith(("https://", "http://")):
        return None, UNAVAILABLE
    public = public_origin(origin)
    base = public or request_origin(origin)
    if public:
        try:
            call_saved(
                client,
                "auth.backend.domains.set",
                {"domains": [public]},
                folder=folder,
                suffix=public,
            )
        except PramaanError as exc:
            return None, startup_notice(exc, UNAVAILABLE)
        return Gate(auth_url, base), ""
    return Gate(auth_url, base), LOCAL_SKIP


def allow_email(email: str | None, allow: str | None = None) -> bool:
    raw = allow if allow is not None else (os.environ.get("ALLOWED_SIGN_IN") or "")
    listed = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not listed:
        return True
    if not email or not str(email).strip():
        return False
    return str(email).strip().lower() in listed


def jwt_payload(token: str) -> dict:
    """Read the token payload. The signature is checked by auth.token.verify."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise PramaanError("unauthorized", SIGN_IN_REQUIRED)
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError):
        raise PramaanError("unauthorized", SIGN_IN_REQUIRED) from None
    if not isinstance(parsed, dict):
        raise PramaanError("unauthorized", SIGN_IN_REQUIRED)
    return parsed


def accept_token(
    client,
    token: str,
    *,
    auth_url: str,
    origin: str,
    allow: str | None = None,
) -> dict:
    """Check the token, then read email from its payload."""
    checked = client.call(
        "auth.token.verify",
        {
            "token": token,
            "audience": request_origin(auth_url),
            "authorizedParties": [request_origin(origin)],
        },
    )
    user_id = checked.get("userId") if isinstance(checked, dict) else None
    if (
        not isinstance(checked, dict)
        or checked.get("valid") is not True
        or not isinstance(user_id, str)
        or not user_id
    ):
        raise PramaanError("unauthorized", SIGN_IN_REQUIRED)
    claims = jwt_payload(token)
    if claims.get("sub") != user_id:
        raise PramaanError("unauthorized", SIGN_IN_REQUIRED)
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        email = None
    raw_allow = allow if allow is not None else (os.environ.get("ALLOWED_SIGN_IN") or "")
    if not allow_email(email, raw_allow):
        raise PramaanError("forbidden", NOT_ALLOWED)
    return {"userId": user_id, "email": email}


def delete_account(client, user_id: str, *, folder: Path | None = None) -> dict:
    """Permanently delete the signed-in account. A spent key means it already ran."""
    key = saved_action_key("auth.user.delete", folder=folder, suffix=user_id)
    try:
        payload = client.call(
            "auth.user.delete",
            {"userId": user_id, "confirm": True},
            idempotency_key=key,
        )
    except PramaanError as exc:
        if exc.code != "conflict":
            raise
        return {"deleted": True}
    if not isinstance(payload, dict) or payload.get("deleted") is not True:
        raise PramaanError("invalid_request", "The account was not deleted.")
    return {"deleted": True}

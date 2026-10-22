"""Pick the database address. A managed address is saved once and never printed."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from ai_caller.pramaan import PramaanError, startup_notice

LOCAL_DATABASE = "data/ai-caller.db"
LOCAL_LINE = "Managed database is not available yet. Using the local database."
MISSING_LINE = "Managed database did not return an address. Using the local database."


_ACTION_KEY_PREFIX = {
    "auth.backend.provision": "managed-signin-",
    "db.postgres.provision": "managed-database-",
    "auth.backend.domains.set": "managed-signin-domains-",
    "auth.user.delete": "managed-signin-delete-",
}


def saved_action_key(capability_id: str, *, folder: Path | None = None, suffix: str = "") -> str:
    """A stable Idempotency-Key, written once under .runtime. Never printed."""
    label = capability_id if not suffix else f"{capability_id} {suffix}"
    path = (folder or Path(".runtime")) / "idempotency"
    current = _read_keys(path)
    saved = current.get(label)
    if saved:
        return saved
    prefix = _ACTION_KEY_PREFIX.get(capability_id, "action-")
    value = prefix + uuid.uuid4().hex
    current[label] = value
    _write_keys(path, current)
    return value


def rotate_action_key(capability_id: str, *, folder: Path | None = None, suffix: str = "") -> str:
    """Replace the saved key with a fresh one and return it."""
    label = capability_id if not suffix else f"{capability_id} {suffix}"
    path = (folder or Path(".runtime")) / "idempotency"
    current = _read_keys(path)
    fresh = _ACTION_KEY_PREFIX.get(capability_id, "action-") + uuid.uuid4().hex
    current[label] = fresh
    _write_keys(path, current)
    return fresh


def call_saved(
    client,
    capability_id: str,
    body: dict,
    *,
    folder: Path | None = None,
    suffix: str = "",
):
    """Call with the saved key. A spent key is replaced once and the call runs again.

    The platform answers a spent key with a conflict once its result was shown.
    These setup calls return the same answer every time, so a fresh key is safe.
    The replacement is saved, so later starts keep one key.
    """
    key = saved_action_key(capability_id, folder=folder, suffix=suffix)
    try:
        return client.call(capability_id, body, idempotency_key=key)
    except PramaanError as exc:
        if exc.code != "conflict":
            raise
    fresh = rotate_action_key(capability_id, folder=folder, suffix=suffix)
    return client.call(capability_id, body, idempotency_key=fresh)


def open_database(client, *, folder: Path | None = None, local: str = LOCAL_DATABASE) -> tuple[str, str]:
    """Return a connection string and one startup line. The line is empty when the managed address is saved."""
    secret = (folder or Path(".runtime")) / "database"
    try:
        payload = call_saved(client, "db.postgres.provision", {}, folder=folder)
    except PramaanError as exc:
        return local, startup_notice(exc, LOCAL_LINE)
    pooled = ""
    direct = ""
    if isinstance(payload, dict):
        if isinstance(payload.get("connectionString"), str):
            pooled = payload["connectionString"]
        if isinstance(payload.get("directConnectionString"), str):
            direct = payload["directConnectionString"]
    if pooled:
        _write_secret(secret, pooled + "\n" + (direct or pooled) + "\n")
        return pooled, ""
    saved = _read_secret(secret)
    if saved:
        return saved, ""
    return local, MISSING_LINE


def _read_keys(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        label, sep, value = line.partition("\t")
        if sep and label and value:
            found[label] = value
    return found


def _write_keys(path: Path, keys: dict[str, str]) -> None:
    text = "".join(f"{label}\t{value}\n" for label, value in keys.items())
    _write_secret(path, text)


def _read_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text().splitlines()
    return lines[0].strip() if lines else ""


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(stat.S_IRWXU)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(handle, "w") as out:
        out.write(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

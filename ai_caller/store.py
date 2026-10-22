"""Local records. The browser never holds the API key. Rows belong to one owner."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Conflict(Exception):
    pass


class DatabaseError(Exception):
    pass


class _Link:
    """One connection. A managed address uses the same statements as the local file."""

    def __init__(self, raw, postgres: bool) -> None:
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql: str, args: tuple = ()):  # noqa: ANN201
        return self.raw.execute(_sql(sql, self.postgres), args)

    def executescript(self, script: str) -> None:
        if not self.postgres:
            self.raw.executescript(script)
            return
        for statement in script.split(";"):
            if statement.strip():
                self.raw.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def _sql(sql: str, postgres: bool) -> str:
    if not postgres:
        return sql
    if sql.startswith("INSERT OR IGNORE"):
        sql = sql.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
    elif sql.startswith("INSERT OR REPLACE"):
        sql = (
            "INSERT INTO idempotency (owner, key, route, fingerprint, status, response) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (owner, key) DO UPDATE SET "
            "route = excluded.route, fingerprint = excluded.fingerprint, "
            "status = excluded.status, response = excluded.response"
        )
    return sql.replace("?", "%s")


def _connect(path: str) -> _Link:
    postgres = path.startswith("postgres://") or path.startswith("postgresql://")
    if not postgres:
        raw = sqlite3.connect(path, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        return _Link(raw, False)
    try:
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(path, row_factory=dict_row, connect_timeout=5)
    except Exception:
        raise DatabaseError("The managed database did not open.") from None
    return _Link(raw, True)


class Store:
    def __init__(self, path: str = ":memory:", owner: str = "local") -> None:
        if not owner:
            raise ValueError("An owner is required.")
        self.path = path
        self.owner = owner
        self._lock = threading.Lock()
        self._db = _connect(path)
        self._create()

    def _create(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
              owner TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL, updated TEXT NOT NULL,
              PRIMARY KEY (owner, name)
            );
            CREATE TABLE IF NOT EXISTS tools (
              owner TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (owner, name)
            );
            CREATE TABLE IF NOT EXISTS squads (
              owner TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (owner, name)
            );
            CREATE TABLE IF NOT EXISTS calls (
              owner TEXT NOT NULL, ref TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL,
              PRIMARY KEY (owner, ref)
            );
            CREATE TABLE IF NOT EXISTS campaigns (
              owner TEXT NOT NULL, ref TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (owner, ref)
            );
            CREATE TABLE IF NOT EXISTS knowledge (
              owner TEXT NOT NULL, ref TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (owner, ref)
            );
            CREATE TABLE IF NOT EXISTS opt_outs (
              owner TEXT NOT NULL, address TEXT NOT NULL, channel TEXT NOT NULL,
              PRIMARY KEY (owner, address, channel)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
              owner TEXT NOT NULL, key TEXT NOT NULL, route TEXT NOT NULL,
              fingerprint TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL,
              PRIMARY KEY (owner, key)
            );
            CREATE TABLE IF NOT EXISTS settings (
              owner TEXT PRIMARY KEY, webhook_endpoint TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS callers (
              owner TEXT NOT NULL, number TEXT NOT NULL, body TEXT NOT NULL,
              PRIMARY KEY (owner, number)
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _dump(self, table: str, key: str, value: str, body: dict, stamp: str | None = None) -> dict:
        columns = "owner, " + key + ", body"
        marks = "?, ?, ?"
        args: tuple = (self.owner, value, json.dumps(body))
        update = "body = excluded.body"
        if stamp:
            columns += ", " + stamp
            marks += ", ?"
            args = args + (_now(),)
            if stamp == "updated":
                update += ", updated = excluded.updated"
        with self._lock:
            self._db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({marks}) "
                f"ON CONFLICT(owner, {key}) DO UPDATE SET {update}",
                args,
            )
            self._db.commit()
        return body

    def save_agent(self, body: dict) -> dict:
        return self._dump("agents", "name", body["name"], body, stamp="updated")

    def list_agents(self) -> list[dict]:
        return self._rows("SELECT body FROM agents WHERE owner = ? ORDER BY name", (self.owner,))

    def get_agent(self, name: str) -> dict | None:
        return self._one("SELECT body FROM agents WHERE owner = ? AND name = ?", (self.owner, name))

    def delete_agent(self, name: str) -> bool:
        return self._delete("DELETE FROM agents WHERE owner = ? AND name = ?", (self.owner, name))

    def save_tool(self, body: dict) -> dict:
        return self._dump("tools", "name", body["name"], body)

    def list_tools(self) -> list[dict]:
        return self._rows("SELECT body FROM tools WHERE owner = ? ORDER BY name", (self.owner,))

    def get_tool(self, name: str) -> dict | None:
        return self._one("SELECT body FROM tools WHERE owner = ? AND name = ?", (self.owner, name))

    def delete_tool(self, name: str) -> bool:
        return self._delete("DELETE FROM tools WHERE owner = ? AND name = ?", (self.owner, name))

    def save_squad(self, body: dict) -> dict:
        return self._dump("squads", "name", body["name"], body)

    def list_squads(self) -> list[dict]:
        return self._rows("SELECT body FROM squads WHERE owner = ? ORDER BY name", (self.owner,))

    def save_call(self, body: dict) -> dict:
        stored = dict(body)
        stored.setdefault("created", _now())
        return self._dump("calls", "ref", stored["ref"], stored, stamp="created")

    def list_calls(self, *, status: str | None = None, assistant: str | None = None, q: str | None = None) -> list[dict]:
        rows = self._rows("SELECT body FROM calls WHERE owner = ? ORDER BY created DESC", (self.owner,))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if assistant:
            rows = [row for row in rows if row.get("assistant") == assistant or row.get("squad") == assistant]
        if q:
            needle = q.lower()
            rows = [row for row in rows if needle in json.dumps(row).lower()]
        return rows

    def get_call(self, ref: str) -> dict | None:
        return self._one("SELECT body FROM calls WHERE owner = ? AND ref = ?", (self.owner, ref))

    def save_caller(self, body: dict) -> dict:
        return self._dump("callers", "number", body["number"], body)

    def list_callers(self) -> list[dict]:
        return self._rows("SELECT body FROM callers WHERE owner = ? ORDER BY number", (self.owner,))

    def delete_caller(self, number: str) -> None:
        self._delete(
            "DELETE FROM callers WHERE owner = ? AND number = ?", (self.owner, number)
        )

    def save_knowledge(self, body: dict) -> dict:
        return self._dump("knowledge", "ref", body["ref"], body)

    def list_knowledge(self) -> list[dict]:
        return self._rows("SELECT body FROM knowledge WHERE owner = ? ORDER BY ref", (self.owner,))

    def get_knowledge(self, ref: str) -> dict | None:
        return self._one("SELECT body FROM knowledge WHERE owner = ? AND ref = ?", (self.owner, ref))

    def delete_knowledge(self, ref: str) -> bool:
        return self._delete("DELETE FROM knowledge WHERE owner = ? AND ref = ?", (self.owner, ref))

    def save_campaign(self, body: dict) -> dict:
        return self._dump("campaigns", "ref", body["ref"], body)

    def list_campaigns(self) -> list[dict]:
        return self._rows("SELECT body FROM campaigns WHERE owner = ?", (self.owner,))

    def get_campaign(self, ref: str) -> dict | None:
        return self._one("SELECT body FROM campaigns WHERE owner = ? AND ref = ?", (self.owner, ref))

    def add_opt_out(self, address: str, channel: str = "voice") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO opt_outs (owner, address, channel) VALUES (?, ?, ?)",
                (self.owner, address, channel),
            )
            self._db.commit()

    def remove_opt_out(self, address: str, channel: str = "voice") -> None:
        self._delete(
            "DELETE FROM opt_outs WHERE owner = ? AND address = ? AND channel = ?",
            (self.owner, address, channel),
        )

    def opted_out(self, address: str, channel: str = "voice") -> bool:
        with self._lock:
            found = self._db.execute(
                "SELECT 1 FROM opt_outs WHERE owner = ? AND address = ? AND channel = ?",
                (self.owner, address, channel),
            ).fetchone()
        return found is not None

    def list_opt_outs(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT address, channel FROM opt_outs WHERE owner = ? ORDER BY address",
                (self.owner,),
            ).fetchall()
        return [{"address": row["address"], "channel": row["channel"]} for row in rows]

    def settings(self) -> dict:
        row = self._one_raw("SELECT webhook_endpoint FROM settings WHERE owner = ?", (self.owner,))
        return {"webhookEndpoint": row["webhook_endpoint"] if row else ""}

    def save_settings(self, webhook_endpoint: str) -> dict:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (owner, webhook_endpoint) VALUES (?, ?) "
                "ON CONFLICT(owner) DO UPDATE SET webhook_endpoint = excluded.webhook_endpoint",
                (self.owner, webhook_endpoint),
            )
            self._db.commit()
        return {"webhookEndpoint": webhook_endpoint}

    def recall(self, key: str, route: str, body: dict) -> tuple[int, dict] | None:
        fingerprint = json.dumps({"route": route, "body": body}, sort_keys=True)
        with self._lock:
            row = self._db.execute(
                "SELECT route, fingerprint, status, response FROM idempotency WHERE owner = ? AND key = ?",
                (self.owner, key),
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise Conflict("This idempotency key was already used.")
        return int(row["status"]), json.loads(row["response"])

    def remember(self, key: str, route: str, body: dict, status: int, payload: dict) -> None:
        fingerprint = json.dumps({"route": route, "body": body}, sort_keys=True)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO idempotency (owner, key, route, fingerprint, status, response) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.owner, key, route, fingerprint, status, json.dumps(payload)),
            )
            self._db.commit()

    def _rows(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            found = self._db.execute(sql, args).fetchall()
        return [json.loads(row["body"]) for row in found]

    def _one(self, sql: str, args: tuple) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _one_raw(self, sql: str, args: tuple) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _delete(self, sql: str, args: tuple) -> bool:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor.rowcount > 0


def agent_record(agent: Any, squads: list[str] | None = None) -> dict:
    record = {
        "name": agent.name,
        "prompt": agent.prompt,
        "greeting": agent.greeting,
        "language": agent.language,
        "maxDurationSeconds": agent.max_duration_seconds,
        "tools": [tool.name for tool in agent.tools],
        "squads": list(squads or []),
    }
    if getattr(agent, "voice", None):
        record["voice"] = agent.voice
    return record

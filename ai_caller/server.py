"""Console. The browser talks to this process. This process talks to Pramaan."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ai_caller.database import LOCAL_DATABASE, LOCAL_LINE, open_database
from ai_caller.model import Agent, DefinitionError, Parameter, Squad, Tool
from ai_caller.pramaan import KEY_MESSAGE, OptOut, OutcomeLater, PramaanError, client_from_env
from ai_caller.service import Service
from ai_caller.sign_in import (
    COOKIE,
    SIGN_IN_REQUIRED,
    accept_token,
    app_origin,
    delete_account,
    open_sign_in,
    request_origin,
)
from ai_caller.store import Conflict, DatabaseError, Store
from ai_caller.webhook import handle_callback

STATIC = Path(__file__).resolve().parent / "static"


def load_env(path: Path | None = None) -> None:
    """Read .env into the process. A value already set in the environment stays."""
    file = path or Path.cwd() / ".env"
    if not file.is_file():
        return
    for line in file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value and key not in os.environ:
            os.environ[key] = value


def _tool_from(raw: dict) -> Tool:
    params = [
        Parameter(
            name=item["name"],
            type=item.get("type", "string"),
            description=item.get("description", ""),
            required=bool(item.get("required", False)),
        )
        for item in raw.get("parameters") or []
    ]
    return Tool(
        name=raw["name"],
        description=raw.get("description") or "A tool.",
        kind=raw.get("kind") or "function",
        parameters=params,
        code=raw.get("code") or None,
        endpoint=raw.get("endpoint") or None,
        timeout_seconds=int(raw.get("timeoutSeconds") or 10),
    )


def _agent_from(body: dict) -> Agent:
    return Agent(
        name=body["name"],
        prompt=body["prompt"],
        greeting=body.get("greeting") or None,
        language=body.get("language") or "en",
        tools=[_tool_from(raw) for raw in body.get("tools") or []],
        knowledge_text=body.get("knowledge") or None,
        knowledge_base_ref=body.get("knowledgeBaseRef") or None,
        voice=body.get("voice") or None,
        max_duration_seconds=int(body.get("maxDurationSeconds") or 120),
    )


def _squads(body: dict) -> list[str]:
    raw = body.get("squads") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    return [str(item) for item in raw]


class Handler(BaseHTTPRequestHandler):
    service: Service
    gate = None
    client = None

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, payload: dict | None = None, body: bytes | None = None, content_type: str = "application/json") -> None:
        data = body if body is not None else json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _json(self, raw: bytes) -> dict:
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        if not isinstance(parsed, dict):
            raise ValueError("Send a JSON object.")
        return parsed

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("The request is too large.")
        return self.rfile.read(length) if length else b""

    def _query(self) -> dict:
        return {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}

    def _act(self, route: str, body: dict, fn) -> None:
        key = self.headers.get("Idempotency-Key")
        if key:
            try:
                prior = self.service.store.recall(key, route, body)
            except Conflict as exc:
                self._error(409, str(exc))
                return
            if prior is not None:
                self._send(prior[0], prior[1])
                return
        status, payload = fn()
        if key:
            self.service.store.remember(key, route, body, status, payload)
        self._send(status, payload)

    def _page(self, name: str) -> None:
        self._send(200, body=(STATIC / name).read_bytes(), content_type="text/html; charset=utf-8")

    def _session_row(self) -> dict | None:
        gate = self.gate
        if gate is None:
            return None
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == COOKIE and value in gate.sessions:
                return gate.sessions[value]
        return None

    def _cookie_secret(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == COOKIE:
                return value
        return ""

    def _allowed(self) -> bool:
        if self.gate is None or self._session_row() is not None:
            return True
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._error(401, SIGN_IN_REQUIRED)
        else:
            self.send_response(302)
            self.send_header("Location", "/sign-in")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    def _sign_in_config(self) -> None:
        gate = self.gate
        if gate is None:
            self._error(404, "Sign in is not in use.")
            return
        self._send(200, {"authUrl": gate.auth_url, "callbackURL": gate.callback})

    def _open_session(self) -> None:
        gate = self.gate
        if gate is None or self.client is None:
            self._error(404, "Sign in is not in use.")
            return
        try:
            body = self._json(self._read_raw())
            token = body.get("token")
            if not isinstance(token, str) or not token or len(token) > 8192:
                raise ValueError(SIGN_IN_REQUIRED)
            who = accept_token(self.client, token, auth_url=gate.auth_url, origin=gate.origin)
        except PramaanError as exc:
            self._error(403 if exc.code == "forbidden" else 401, str(exc))
            return
        except (ValueError, json.JSONDecodeError):
            self._error(401, SIGN_IN_REQUIRED)
            return
        secret = gate.open_session(who["userId"], who.get("email"))
        secure = "; Secure" if gate.origin.startswith("https://") else ""
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", f"{COOKIE}={secret}; HttpOnly; Path=/; SameSite=Lax{secure}")
        self.end_headers()
        self.wfile.write(data)

    def _send_cleared(self, status: int, payload: dict) -> None:
        gate = self.gate
        secure = "; Secure" if gate is not None and gate.origin.startswith("https://") else ""
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", f"{COOKIE}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax{secure}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/sign-in":
            self._page("sign-in.html")
            return
        if path == "/sign-in/callback":
            self._page("callback.html")
            return
        if path == "/api/sign-in":
            self._sign_in_config()
            return
        if path.startswith("/static/"):
            self._static(path.removeprefix("/static/"))
            return
        if not self._allowed():
            return
        try:
            if path in {"/", "/index.html"}:
                self._send(200, body=(STATIC / "index.html").read_bytes(), content_type="text/html; charset=utf-8")
                return
            if path == "/api/session":
                row = self._session_row()
                if row is None:
                    self._send(200, {"signedIn": False})
                else:
                    self._send(200, {"signedIn": True, "userId": row["userId"], "email": row.get("email") or ""})
                return
            if path == "/api/agents":
                self._send(200, {"items": self.service.list_agents()})
            elif path == "/api/knowledge":
                self._send(200, {"items": self.service.list_knowledge()})
            elif path.startswith("/api/knowledge/"):
                ref = path.removeprefix("/api/knowledge/")
                self._send(200, self.service.get_knowledge(ref))
            elif path == "/api/tools":
                self._send(200, {"items": self.service.list_tools()})
            elif path == "/api/squads":
                self._send(200, {"items": self.service.list_squads()})
            elif path == "/api/calls":
                query = self._query()
                self._send(
                    200,
                    {
                        "items": self.service.list_calls(
                            status=query.get("status") or None,
                            assistant=query.get("assistant") or None,
                            q=query.get("q") or None,
                        )
                    },
                )
            elif path == "/api/campaigns":
                self._send(200, {"items": self.service.list_campaigns()})
            elif path == "/api/numbers/callers":
                self._send(200, {"items": self.service.list_caller_numbers()})
            elif path == "/api/numbers":
                self._send(200, self.service.list_numbers())
            elif path == "/api/opt-outs":
                self._send(200, {"items": self.service.list_opt_outs()})
            elif path == "/api/settings":
                self._send(200, self.service.settings_view())
            elif path == "/api/transcripts":
                rows = [
                    {"ref": item.get("ref"), "transcript": item.get("transcript") or ""}
                    for item in self.service.list_calls()
                ]
                self._send(200, {"items": rows})
            elif path.startswith("/api/calls/") and path.endswith("/recording"):
                ref = path.removeprefix("/api/calls/").removesuffix("/recording")
                if "/" in ref:
                    self._error(404, "Not found.")
                    return
                self._send(200, body=self.service.recording(ref), content_type="audio/wav")
            elif path.startswith("/api/calls/"):
                ref = path.removeprefix("/api/calls/")
                if "/" in ref:
                    self._error(404, "Not found.")
                    return
                detail = self.service.call_detail(ref)
                if detail is None:
                    self._error(404, "Not found.")
                    return
                self._send(200, detail)
            elif path.startswith("/api/campaigns/") and path.endswith("/results"):
                ref = path.removeprefix("/api/campaigns/").removesuffix("/results")
                row = self.service.store.get_campaign(ref)
                if row is None:
                    self._error(404, "Not found.")
                    return
                self._send(200, {"items": row.get("rows") or []})
            else:
                self._error(404, "Not found.")
        except KeyError:
            self._error(404, "Not found.")
        except PramaanError as exc:
            self._error(400, str(exc))

    def _static(self, name: str) -> None:
        if name.startswith("fonts/"):
            leaf = name.removeprefix("fonts/")
            if "/" in leaf or leaf.startswith(".") or not leaf.endswith(".woff2"):
                self._error(404, "Not found.")
                return
            file = STATIC / "fonts" / leaf
            kind = "font/woff2"
        else:
            if "/" in name or name.startswith("."):
                self._error(404, "Not found.")
                return
            file = STATIC / name
            kind = "text/css" if name.endswith(".css") else "text/javascript"
        if not file.is_file():
            self._error(404, "Not found.")
            return
        self._send(200, body=file.read_bytes(), content_type=kind)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/session":
            self._open_session()
            return
        if path == "/api/sign-out":
            if self.gate is not None:
                self.gate.sessions.pop(self._cookie_secret(), None)
            self._send_cleared(200, {"ok": True})
            return
        if path != "/tools/callback" and not self._allowed():
            return
        try:
            raw = self._read_raw()
        except ValueError as exc:
            self._error(400, str(exc))
            return
        if path == "/tools/callback":
            status, payload = handle_callback(
                raw,
                secret=os.environ.get("PRAMAAN_WEBHOOK_SECRET") or "",
                webhook_id=self.headers.get("webhook-id") or "",
                timestamp=self.headers.get("webhook-timestamp") or "",
                signature=self.headers.get("webhook-signature") or "",
            )
            self._send(status, payload)
            return
        try:
            body = self._json(raw)
            self._post(path, body)
        except (DefinitionError, ValueError, KeyError) as exc:
            self._error(400, str(exc) or "Check the form and try again.")
        except OptOut as exc:
            self._error(409, str(exc))
        except OutcomeLater as exc:
            self._error(429, str(exc))
        except PramaanError as exc:
            self._error(400, str(exc))

    def _post(self, path: str, body: dict) -> None:
        if path == "/api/account/delete":
            if body.get("confirm") is not True:
                raise ValueError("Confirm before this account is deleted.")
            row = self._session_row()
            if row is None or self.client is None:
                self._error(404, "Sign in is not in use.")
                return
            delete_account(self.client, row["userId"])
            if self.gate is not None:
                self.gate.sessions.pop(self._cookie_secret(), None)
            self._send_cleared(200, {"deleted": True})
        elif path == "/api/agents":
            self._act(path, body, lambda: (200, self.service.publish(_agent_from(body), _squads(body))))
        elif path == "/api/knowledge":
            if body.get("confirm") is not True:
                raise ValueError("Confirm before the document is indexed.")
            self._act(
                path,
                body,
                lambda: (200, self.service.create_knowledge(body["name"], body.get("text") or "")),
            )
        elif path == "/api/tools":
            self._act(path, body, lambda: (200, self.service.save_tool(body)))
        elif path == "/api/tools/test":
            self._send(200, self.service.test_tool(body["name"], body.get("arguments") or {}))
        elif path == "/api/squads":
            members = []
            for item in body.get("members") or []:
                if isinstance(item, str):
                    members.append((item, []))
                else:
                    members.append((item["assistant"], list(item.get("handoffTo") or [])))
            self._act(path, body, lambda: (200, self.service.publish_squad(Squad(body["name"], members))))
        elif path == "/api/calls/estimate":
            self._send(
                200,
                self.service.estimate_agent_call(
                    to=body["to"],
                    from_number=body["from"],
                    assistant=body.get("assistant") or None,
                    squad=body.get("squad") or None,
                    max_duration_seconds=int(body.get("maxDurationSeconds") or 120),
                    variables=body.get("variables") or {},
                    schedule_at=body.get("scheduleAt") or None,
                ),
            )
        elif path == "/api/calls":
            if body.get("confirm") is not True:
                raise ValueError("Confirm the call before it is placed.")
            self._act(
                path,
                body,
                lambda: (
                    200,
                    self.service.place_agent_call(
                        to=body["to"],
                        from_number=body["from"],
                        assistant=body.get("assistant") or None,
                        squad=body.get("squad") or None,
                        max_duration_seconds=int(body.get("maxDurationSeconds") or 120),
                        variables=body.get("variables") or {},
                        schedule_at=body.get("scheduleAt") or None,
                        plan=body.get("plan") or None,
                        outcome_body=body.get("outcomeBody") or None,
                    ),
                ),
            )
        elif path.startswith("/api/calls/") and path.endswith("/summary"):
            ref = path.removeprefix("/api/calls/").removesuffix("/summary")
            if not ref or "/" in ref:
                self._error(404, "Not found.")
                return
            try:
                self._act(path, body, lambda: (200, self.service.retry_summary(ref)))
            except KeyError:
                self._error(404, "Not found.")
        elif path.startswith("/api/calls/") and path.endswith("/end"):
            if body.get("confirm") is not True:
                raise ValueError("Confirm before ending the call.")
            ref = path.removeprefix("/api/calls/").removesuffix("/end")
            try:
                self._act(path, body, lambda: (200, self.service.end_call(ref)))
            except KeyError:
                self._error(404, "Not found.")
        elif path == "/api/numbers/search":
            self._send(200, self.service.search_numbers(body.get("country") or "US", body.get("areaCode")))
        elif path == "/api/numbers/estimate":
            self._send(
                200,
                self.service.estimate_number_setup(
                    body.get("country") or "US",
                    body.get("type") or "local",
                    body.get("phoneNumber") or None,
                    body.get("areaCode") or None,
                ),
            )
        elif path == "/api/numbers/purchase":
            if body.get("confirm") is not True:
                raise ValueError("Confirm the purchase before a number is rented.")
            estimate = body.get("estimate")
            if isinstance(estimate, dict):
                self._act(path, body, lambda: (200, self.service.confirm_number_setup(estimate)))
            else:
                self._act(
                    path,
                    body,
                    lambda: (200, self.service.buy_number(body.get("country") or "US", body.get("type") or "local")),
                )
        elif path == "/api/numbers/callers":
            if body.get("confirm") is not True:
                raise ValueError("Confirm before this number is used for agent calls.")
            self._act(
                path,
                body,
                lambda: (
                    200,
                    self.service.import_caller_number(
                        body["number"],
                        name=body.get("name") or None,
                        assistant=body.get("assistant") or None,
                        confirm=True,
                    ),
                ),
            )
        elif path == "/api/numbers/callers/disconnect":
            if body.get("confirm") is not True:
                raise ValueError("Confirm before this caller ID is disconnected.")
            self._act(
                path,
                body,
                lambda: (
                    200,
                    self.service.disconnect_caller_number(body["number"], confirm=True),
                ),
            )
        elif path == "/api/numbers/included":
            self._act(
                path,
                body,
                lambda: (200, self.service.create_included_number(body.get("areaCode") or None)),
            )
        elif path == "/api/numbers/attach":
            self._error(409, "Inbound attach is not yet live.")
        elif path == "/api/campaigns/csv":
            self._send(200, {"rows": self.service.rows_from_csv(body.get("csv") or "")})
        elif path == "/api/campaigns":
            if body.get("confirm") is not True:
                raise ValueError("Confirm the campaign before anyone is called.")
            rows = body.get("rows") or []
            if body.get("csv"):
                rows = self.service.rows_from_csv(body["csv"])
            if body.get("assistant"):
                self._act(
                    path,
                    body,
                    lambda: (
                        200,
                        self.service.start_agent_campaign(
                            body["name"],
                            body["from"],
                            body["assistant"],
                            rows,
                            int(body.get("maxDurationSeconds") or 120),
                        ),
                    ),
                )
            else:
                self._act(
                    path,
                    body,
                    lambda: (
                        200,
                        self.service.start_campaign(body["name"], body["from"], body["message"], rows),
                    ),
                )
        elif path.startswith("/api/campaigns/") and path.endswith("/pause"):
            ref = path.removeprefix("/api/campaigns/").removesuffix("/pause")
            self._act(path, body, lambda: (200, self.service.pause_campaign(ref)))
        elif path.startswith("/api/campaigns/") and path.endswith("/resume"):
            ref = path.removeprefix("/api/campaigns/").removesuffix("/resume")
            self._act(path, body, lambda: (200, self.service.resume_campaign(ref)))
        elif path.startswith("/api/campaigns/") and path.endswith("/finish"):
            ref = path.removeprefix("/api/campaigns/").removesuffix("/finish")
            self._act(path, body, lambda: (200, self.service.finish_campaign(ref)))
        elif path == "/api/opt-outs":
            self._act(path, body, lambda: (200, self.service.opt_out(body["address"])))
        elif path == "/api/opt-outs/remove":
            if body.get("confirm") is not True:
                raise ValueError("Confirm before this number can be called again.")
            self._act(path, body, lambda: (200, self.service.remove_opt_out(body["address"])))
        elif path == "/api/settings":
            self._act(path, body, lambda: (200, self.service.save_settings(body.get("webhookEndpoint") or "")))
        else:
            self._error(404, "Not found.")

    def do_PUT(self) -> None:
        if not self._allowed():
            return
        path = urlparse(self.path).path
        try:
            body = self._json(self._read_raw())
            if not path.startswith("/api/agents/"):
                self._error(404, "Not found.")
                return
            name = path.removeprefix("/api/agents/")
            body["name"] = name
            self._act(path, body, lambda: (200, self.service.update_agent(_agent_from(body), _squads(body))))
        except (DefinitionError, ValueError) as exc:
            self._error(400, str(exc) or "Check the form and try again.")
        except KeyError:
            self._error(404, "Not found.")
        except PramaanError as exc:
            self._error(400, str(exc))

    def do_DELETE(self) -> None:
        if not self._allowed():
            return
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/agents/"):
                self._send(200, self.service.delete_agent(path.removeprefix("/api/agents/")))
            elif path.startswith("/api/tools/"):
                self._send(200, self.service.delete_tool(path.removeprefix("/api/tools/")))
            elif path.startswith("/api/knowledge/"):
                body = self._json(self._read_raw())
                if body.get("confirm") is not True:
                    raise ValueError("Confirm before this document base is deleted.")
                ref = unquote(path.removeprefix("/api/knowledge/"))
                self._send(200, self.service.delete_knowledge(ref, confirm=True))
            else:
                self._error(404, "Not found.")
        except (DefinitionError, ValueError) as exc:
            self._error(400, str(exc))
        except KeyError:
            self._error(404, "Not found.")
        except PramaanError as exc:
            self._error(400, str(exc))


def make_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    store: Store | None = None,
    client=None,
    gate=None,
) -> ThreadingHTTPServer:
    client = client or client_from_env()
    handler = type(
        "BoundHandler",
        (Handler,),
        {"service": Service(client, store), "gate": gate, "client": client},
    )
    return ThreadingHTTPServer((host, port), handler)


def _ready_store(client, *, folder: Path | None = None) -> tuple[Store, str]:
    local = os.environ.get("AI_CALLER_DB") or LOCAL_DATABASE
    if os.environ.get("PRAMAAN_API_KEY"):
        chosen, notice = open_database(client, folder=folder, local=local)
    else:
        chosen, notice = local, ""
    try:
        store = _open_store(chosen)
    except DatabaseError:
        notice = LOCAL_LINE
        store = _open_store(local)
    return store, notice


def _open_store(path: str) -> Store:
    if path != ":memory:" and not path.startswith(("postgres://", "postgresql://")):
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return Store(path)


def main() -> None:
    load_env()
    port = int(os.environ.get("PORT") or 8787)
    client = client_from_env()
    store, notice = _ready_store(client)
    if notice:
        print(notice, flush=True)
    gate, sign_notice = open_sign_in(client, port=port)
    if sign_notice:
        print(sign_notice, flush=True)
    if os.environ.get("PRAMAAN_FAKE") != "1" and not os.environ.get("PRAMAAN_API_KEY"):
        print(KEY_MESSAGE, flush=True)
    server = make_server(port=port, store=store, client=client, gate=gate)
    print(f"AI Caller console at {request_origin(app_origin(port))}", flush=True)
    server.serve_forever()

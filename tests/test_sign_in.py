"""Managed sign-in on the fake client. A local address is not registered."""

import base64
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from ai_caller.pramaan import IDEMPOTENCY_REQUIRED, FakePramaan, PramaanError
from ai_caller.server import make_server
from ai_caller.sign_in import (
    COOKIE,
    LOCAL_SKIP,
    UNAVAILABLE,
    Gate,
    accept_token,
    app_origin,
    open_sign_in,
    prepare_sign_in,
)
from ai_caller.store import Store


def _jwt(user_id: str = "user_1", email: str | None = "Ada@Example.com", *,
         sub: str | None = None) -> str:
    payload = {"sub": user_id if sub is None else sub}
    if email is not None:
        payload["email"] = email
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.c2lnbmF0dXJl"


GOOD = _jwt()


class SignInTests(unittest.TestCase):
    def _ready(self) -> FakePramaan:
        fake = FakePramaan()
        fake.sign_in_mode = "ready"
        fake.sign_in_tokens[GOOD] = {"valid": True, "userId": "user_1", "sessionId": "sess_1"}
        fake.sign_in_users["user_1"] = {"email": "Ada@Example.com"}
        return fake

    def test_a_local_address_is_not_sent(self) -> None:
        skipped = [
            "http://127.0.0.1:8787",
            "http://localhost:8787",
            "https://localhost",
            "https://127.0.0.1:8787",
            "https://api.localhost",
            "https://10.1.2.3",
            "https://app",
            "https://app.example.com/console",
            "https://app.example.com?x=1",
            "https://app.example.com#top",
            "https://user:pass@app.example.com",
            "https://app.example.com.",
            "https://*.example.com",
        ]
        for origin in skipped:
            fake = self._ready()
            printed = io.StringIO()
            with redirect_stdout(printed):
                gate, notice = prepare_sign_in(fake, origin=origin)
            self.assertIsNotNone(gate, origin)
            self.assertEqual(notice, LOCAL_SKIP, origin)
            self.assertEqual([item["id"] for item in fake.calls], ["auth.backend.provision"], origin)
            self.assertNotIn(fake.sign_in_auth_url, printed.getvalue())
            self.assertNotIn("jwks", printed.getvalue())

    def test_a_public_https_address_is_sent_once_per_start(self) -> None:
        fake = self._ready()
        first, notice = prepare_sign_in(fake, origin="https://app.example.com/")
        second, again = prepare_sign_in(fake, origin="https://app.example.com:8443")
        self.assertEqual(notice, "")
        self.assertEqual(again, "")
        self.assertEqual(first.auth_url, fake.sign_in_auth_url)
        self.assertEqual(second.callback, "https://app.example.com:8443/sign-in/callback")
        domains = [item for item in fake.calls if item["id"] == "auth.backend.domains.set"]
        self.assertEqual(
            [item["body"] for item in domains],
            [{"domains": ["https://app.example.com"]}, {"domains": ["https://app.example.com:8443"]}],
        )
        keys = [item["idempotency_key"] for item in fake.calls]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(keys))

    def test_unavailable_leaves_the_console_open(self) -> None:
        fake = FakePramaan()
        gate, notice = prepare_sign_in(fake, origin="https://app.example.com")
        self.assertIsNone(gate)
        self.assertEqual(notice, UNAVAILABLE)
        self.assertEqual([item["id"] for item in fake.calls], ["auth.backend.provision"])

    def test_no_key_does_not_provision(self) -> None:
        previous = os.environ.pop("PRAMAAN_API_KEY", None)
        try:
            fake = self._ready()
            gate, notice = open_sign_in(fake, port=8787)
            self.assertIsNone(gate)
            self.assertEqual(notice, "")
            self.assertEqual(fake.calls, [])
            self.assertEqual(app_origin(8787), "http://localhost:8787")
        finally:
            if previous is None:
                os.environ.pop("PRAMAAN_API_KEY", None)
            else:
                os.environ["PRAMAAN_API_KEY"] = previous

    def test_a_key_uses_the_app_address(self) -> None:
        previous_key = os.environ.get("PRAMAAN_API_KEY")
        previous_url = os.environ.get("APP_URL")
        os.environ["PRAMAAN_API_KEY"] = "test-key"
        os.environ["APP_URL"] = "http://127.0.0.1:8787"
        try:
            fake = self._ready()
            gate, notice = open_sign_in(fake, port=9)
            self.assertEqual(notice, LOCAL_SKIP)
            self.assertEqual(gate.origin, "http://127.0.0.1:8787")
            self.assertNotIn("auth.backend.domains.set", [item["id"] for item in fake.calls])
        finally:
            self._restore("PRAMAAN_API_KEY", previous_key)
            self._restore("APP_URL", previous_url)

    def test_verify_accepts_a_known_token_without_an_allow_list(self) -> None:
        fake = self._ready()
        issued = fake.call("auth.backend.provision", {}, idempotency_key="provision-1")
        self.assertEqual(set(issued), {"status", "authUrl", "jwksUrl"})
        self.assertEqual(fake.sign_in_domains, [])
        who = accept_token(
            fake,
            GOOD,
            auth_url=fake.sign_in_auth_url,
            origin="http://localhost:8787",
            allow="",
        )
        self.assertEqual(who, {"userId": "user_1", "email": "Ada@Example.com"})
        verify = [item for item in fake.calls if item["id"] == "auth.token.verify"][-1]
        self.assertIsNone(verify["idempotency_key"])
        checked = fake.call("auth.token.verify", {"token": GOOD})
        self.assertEqual(
            set(checked),
            {"valid", "userId", "sessionId", "orgId", "expiresAt"},
        )
        self.assertNotIn("email", checked)
        self.assertEqual(
            verify["body"],
            {
                "token": GOOD,
                "audience": "https://sign-in.example.test",
                "authorizedParties": ["http://localhost:8787"],
            },
        )
        self.assertNotIn("auth.user.get", [item["id"] for item in fake.calls])

    def test_verify_refuses_an_unknown_token(self) -> None:
        fake = self._ready()
        with self.assertRaises(PramaanError) as caught:
            accept_token(
                fake,
                GOOD + "x",
                auth_url=fake.sign_in_auth_url,
                origin="http://localhost:8787",
                allow="ada@example.com",
            )
        self.assertEqual(caught.exception.code, "unauthorized")
        altered = fake.call("auth.token.verify", {"token": GOOD + "x"})
        self.assertEqual(altered, {"valid": False})
        self.assertNotIn("auth.user.get", [item["id"] for item in fake.calls])

    def test_the_payload_subject_must_match_the_verified_user(self) -> None:
        fake = self._ready()
        mismatch = _jwt(sub="user_2")
        fake.sign_in_tokens[mismatch] = {"valid": True, "userId": "user_1"}
        with self.assertRaises(PramaanError) as caught:
            accept_token(
                fake,
                mismatch,
                auth_url=fake.sign_in_auth_url,
                origin="http://localhost:8787",
                allow="",
            )
        self.assertEqual(caught.exception.code, "unauthorized")

    def test_allow_list_matches_email_without_case(self) -> None:
        fake = self._ready()
        who = accept_token(
            fake,
            GOOD,
            auth_url=fake.sign_in_auth_url,
            origin="https://app.example.com",
            allow="ada@example.com, other@example.com",
        )
        self.assertEqual(who["email"], "Ada@Example.com")
        self.assertNotIn("auth.user.get", [item["id"] for item in fake.calls])

    def test_allow_list_refuses_someone_else(self) -> None:
        fake = self._ready()
        with self.assertRaises(PramaanError) as caught:
            accept_token(
                fake,
                GOOD,
                auth_url=fake.sign_in_auth_url,
                origin="https://app.example.com",
                allow="other@example.com",
            )
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(str(caught.exception), "This account is not allowed.")

    def _restore(self, name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_pages_keep_the_provider_id_in_the_script_only(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ai_caller" / "static"
        pages = (root / "sign-in.html").read_text() + (root / "callback.html").read_text()
        self.assertNotIn("google", pages.lower())
        script = (root / "sign-in.js").read_text()
        self.assertIn('provider: "google"', script)
        self.assertIn("createAuthClient", script)
        self.assertIn("signIn.email", script)
        self.assertIn("signUp.email", script)
        self.assertIn('fetch(root + "/token"', script)
        self.assertIn('credentials: "include"', script)
        self.assertIn("issuedBody.token", script)
        self.assertNotIn("set-auth-jwt", script)
        self.assertNotIn("getSession", script)
        bundle = (root / "auth-client.js").read_text().lower()
        self.assertIn("createauthclient", bundle)
        self.assertNotIn("google", bundle)
        self.assertNotIn("vercel", bundle)

    def test_a_repeated_start_reuses_the_saved_key(self) -> None:
        import tempfile

        fake = self._ready()
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            first, notice = prepare_sign_in(fake, origin="https://app.example.com", folder=folder)
            second, again = prepare_sign_in(fake, origin="https://app.example.com", folder=folder)
            self.assertEqual(notice, "")
            self.assertEqual(again, "")
            self.assertEqual(first.auth_url, second.auth_url)
            provisions = [item for item in fake.calls if item["id"] == "auth.backend.provision"]
            domains = [item for item in fake.calls if item["id"] == "auth.backend.domains.set"]
            self.assertEqual(len(provisions), 1)
            self.assertEqual(len(domains), 1)
            self.assertTrue(provisions[0]["idempotency_key"].startswith("managed-signin-"))
            self.assertTrue(domains[0]["idempotency_key"].startswith("managed-signin-domains-"))
            text = (folder / "idempotency").read_text()
            self.assertIn(provisions[0]["idempotency_key"], text)
            self.assertNotIn(provisions[0]["idempotency_key"], notice + again)

    def test_a_missing_key_is_printed_and_sign_in_stays_off(self) -> None:
        import tempfile

        class Refuse:
            def call(self, *_args, **_kwargs):
                raise PramaanError("invalid_request", IDEMPOTENCY_REQUIRED)

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            printed = io.StringIO()
            with redirect_stdout(printed):
                gate, notice = prepare_sign_in(
                    Refuse(), origin="http://localhost:8787", folder=folder
                )
            self.assertIsNone(gate)
            self.assertEqual(notice, IDEMPOTENCY_REQUIRED)
            key = (folder / "idempotency").read_text().split("\t", 1)[1].strip()
            self.assertNotIn(key, notice)
            self.assertNotIn(key, printed.getvalue())
            self.assertNotIn("google", notice.lower())

    def test_a_spent_sign_in_key_is_rotated_and_the_gate_still_opens(self) -> None:
        """The platform shows a spent key's result once, then answers conflict."""
        import tempfile

        from ai_caller.database import saved_action_key

        fake = self._ready()
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            spent = saved_action_key("auth.backend.provision", folder=folder)

            class Spent:
                def call(self, capability_id: str, body=None, *, idempotency_key=None):
                    if idempotency_key == spent:
                        raise PramaanError("conflict", "The request failed.")
                    return fake.call(capability_id, body, idempotency_key=idempotency_key)

            gate, notice = prepare_sign_in(Spent(), origin="https://app.example.com", folder=folder)
            self.assertIsNotNone(gate)
            self.assertEqual(notice, "")
            saved = (folder / "idempotency").read_text()
            self.assertNotIn(spent, saved)
            runs = [item for item in fake.calls if item["id"] == "auth.backend.provision"]
            self.assertEqual(len(runs), 1)
            self.assertNotEqual(runs[0]["idempotency_key"], spent)

    def test_a_spent_delete_key_counts_as_deleted(self) -> None:
        import tempfile

        from ai_caller.database import saved_action_key
        from ai_caller.sign_in import delete_account

        fake = self._ready()
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            spent = saved_action_key("auth.user.delete", folder=folder, suffix="user_1")

            class Spent:
                def call(self, capability_id: str, body=None, *, idempotency_key=None):
                    if idempotency_key == spent:
                        raise PramaanError("conflict", "The request failed.")
                    return fake.call(capability_id, body, idempotency_key=idempotency_key)

            self.assertEqual(delete_account(Spent(), "user_1", folder=folder), {"deleted": True})
            self.assertEqual(
                [item for item in fake.calls if item["id"] == "auth.user.delete"], []
            )

    def test_delete_reuses_one_key_per_account(self) -> None:
        import tempfile

        from ai_caller.sign_in import delete_account

        fake = self._ready()
        fake.sign_in_users["user_2"] = {"email": "bea@example.com"}
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            delete_account(fake, "user_1", folder=folder)
            delete_account(fake, "user_1", folder=folder)
            delete_account(fake, "user_2", folder=folder)
            deleted = [item for item in fake.calls if item["id"] == "auth.user.delete"]
            self.assertEqual(len(deleted), 2)
            self.assertNotEqual(deleted[0]["idempotency_key"], deleted[1]["idempotency_key"])
            self.assertTrue(
                all(item["idempotency_key"].startswith("managed-signin-delete-") for item in deleted)
            )
            self.assertEqual(deleted[0]["body"], {"userId": "user_1", "confirm": True})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(base, path, body=None, cookie=None, method=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    method = method or ("POST" if body is not None else "GET")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req) as res:
            raw = res.read()
            kind = res.headers.get("Content-Type") or ""
            payload = json.loads(raw.decode()) if "json" in kind else raw
            return res.status, payload, res.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        exc.close()
        try:
            payload = json.loads(raw.decode())
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload, exc.headers


class SignInRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PRAMAAN_FAKE"] = "1"
        os.environ.pop("PRAMAAN_API_KEY", None)
        os.environ.pop("ALLOWED_SIGN_IN", None)
        self.fake = FakePramaan()
        self.fake.sign_in_mode = "ready"
        self.fake.sign_in_tokens[GOOD] = {"valid": True, "userId": "user_1", "sessionId": "sess_1"}
        self.fake.sign_in_users["user_1"] = {"email": "Ada@Example.com"}
        self.gate = Gate(self.fake.sign_in_auth_url, "http://127.0.0.1:8787")
        self.server = make_server(port=0, store=Store(), client=self.fake, gate=self.gate)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("ALLOWED_SIGN_IN", None)

    def test_the_console_requires_the_local_session(self) -> None:
        status, payload, headers = _http(self.base, "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/sign-in")
        status, payload, _headers = _http(self.base, "/api/agents")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "Sign in to continue."})
        status, payload, _headers = _http(
            self.base, "/api/agents/reception", {"prompt": "Hello."}, method="PUT"
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "Sign in to continue."})
        status, page, _headers = _http(self.base, "/sign-in")
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", page)
        self.assertNotIn(b"google", page.lower())
        status, config, _headers = _http(self.base, "/api/sign-in")
        self.assertEqual(config["authUrl"], self.fake.sign_in_auth_url)
        self.assertEqual(config["callbackURL"], "http://127.0.0.1:8787/sign-in/callback")

    def test_a_verified_token_opens_a_local_session(self) -> None:
        status, payload, headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        cookie = headers.get("Set-Cookie") or ""
        self.assertIn(COOKIE + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Secure", cookie)
        secret = cookie.split(";", 1)[0]
        self.assertNotIn(GOOD, secret)
        status, session, _headers = _http(self.base, "/api/session", cookie=secret)
        self.assertEqual(session, {"signedIn": True, "userId": "user_1", "email": "Ada@Example.com"})
        status, payload, _headers = _http(self.base, "/api/agents", cookie=secret)
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        verify = [item for item in self.fake.calls if item["id"] == "auth.token.verify"]
        self.assertEqual(len(verify), 1)
        self.assertNotIn("auth.user.get", [item["id"] for item in self.fake.calls])

    def test_a_refused_token_does_not_open_a_session(self) -> None:
        status, payload, headers = _http(self.base, "/api/session", {"token": "missing"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "Sign in to continue.")
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_the_allow_list_refuses_someone_else(self) -> None:
        os.environ["ALLOWED_SIGN_IN"] = "other@example.com"
        status, payload, headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "This account is not allowed.")
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertNotIn("auth.user.get", [item["id"] for item in self.fake.calls])

    def test_a_public_session_cookie_is_secure(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        gate = Gate(self.fake.sign_in_auth_url, "https://app.example.com")
        self.server = make_server(port=0, store=Store(), client=self.fake, gate=gate)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        _status, _payload, headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertIn("Secure", headers.get("Set-Cookie") or "")

    def test_a_tool_callback_stays_open(self) -> None:
        status, payload, _headers = _http(self.base, "/tools/callback", {})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "The signature is not valid.")

    def test_without_a_gate_the_console_stays_open(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server = make_server(port=0, store=Store(), client=self.fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        status, payload, _headers = _http(self.base, "/api/agents")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        status, payload, _headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertEqual(status, 404)
        self.assertEqual(self.fake.calls, [])
        status, session, _headers = _http(self.base, "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session, {"signedIn": False})

    def test_delete_requires_confirm_and_then_removes_the_account(self) -> None:
        status, _payload, headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertEqual(status, 200)
        secret = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        status, payload, _headers = _http(self.base, "/api/account/delete", {}, cookie=secret)
        self.assertEqual(status, 400)
        self.assertNotIn("auth.user.delete", [item["id"] for item in self.fake.calls])
        status, payload, headers = _http(
            self.base, "/api/account/delete", {"confirm": True}, cookie=secret
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": True})
        self.assertIn("Max-Age=0", headers.get("Set-Cookie") or "")
        deleted = [item for item in self.fake.calls if item["id"] == "auth.user.delete"]
        self.assertEqual(deleted[-1]["body"], {"userId": "user_1", "confirm": True})
        status, payload, _headers = _http(self.base, "/api/agents", cookie=secret)
        self.assertEqual(status, 401)

    def test_sign_out_clears_the_local_session(self) -> None:
        status, _payload, headers = _http(self.base, "/api/session", {"token": GOOD})
        self.assertEqual(status, 200)
        secret = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        status, payload, headers = _http(self.base, "/api/sign-out", {}, cookie=secret)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertIn("Max-Age=0", headers.get("Set-Cookie") or "")
        status, payload, _headers = _http(self.base, "/api/agents", cookie=secret)
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "Sign in to continue."})

    def test_an_action_without_an_idempotency_key_is_refused(self) -> None:
        fake = FakePramaan()
        body = {
            "ref": "call_1",
            "confirm": True,
            "userId": "user_1",
            "domains": ["https://app.example.com"],
        }
        for capability_id in (
            "auth.backend.provision",
            "db.postgres.provision",
            "auth.backend.domains.set",
            "auth.user.delete",
            "voice.call.end",
        ):
            with self.assertRaises(PramaanError) as caught:
                fake.call(capability_id, body)
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(str(caught.exception), IDEMPOTENCY_REQUIRED)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()

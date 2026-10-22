import json
import time
import unittest

from ai_caller.compile import tool_body
from ai_caller.model import Agent, Tool
from ai_caller.pramaan import TOOL_ENDPOINT_ON_MAIN, FakePramaan, PramaanClient
from ai_caller.service import Service
from ai_caller.webhook import REPLAY_ERROR, handle_callback, register_tool, reset_callback_state, sign

SECRET = "whsec_MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
NOW = 1_800_000_000.0
ENDPOINT = "we_0123456789abcdef0123456789abcdef"


def _post(body: dict, *, secret: str = SECRET, signature: str | None = None, timestamp: str | None = None):
    raw = json.dumps(body).encode()
    stamp = timestamp if timestamp is not None else str(int(NOW))
    webhook_id = str(body.get("idempotencyKey") or "")
    signed = signature if signature is not None else sign(SECRET, webhook_id, stamp, raw)
    return handle_callback(
        raw,
        secret=secret,
        webhook_id=webhook_id,
        timestamp=stamp,
        signature=signed,
        now=NOW,
    )


class CallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_callback_state()

    def test_guide_example(self):
        raw = (
            b'{"callRef":"rh_EXAMPLE","tool":"lookup_order","arguments":{"orderId":"A1","express":true},'
            b'"idempotencyKey":"vtc_5f2b1c0d9e8a7b6c5d4e3f2a1b0c9d8e"}'
        )
        status, payload = handle_callback(
            raw,
            secret=SECRET,
            webhook_id="vtc_5f2b1c0d9e8a7b6c5d4e3f2a1b0c9d8e",
            timestamp="1791763200",
            signature="v1,vJF40DxwuKzA6LmvaXIqPmQvz1i2aSM9vpBOTxgCpcE=",
            now=1791763200,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "Received.")

    def test_good_signature(self):
        status, payload = _post(
            {"callRef": "call_1", "tool": "lookup", "arguments": {"account": "a", "empty": None}, "idempotencyKey": "k1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "Received.")

    def test_bad_signature(self):
        status, payload = _post(
            {"callRef": "call_1", "tool": "lookup", "arguments": {}, "idempotencyKey": "k1"},
            signature="v1,bm90LXRoZS1kaWdlc3Q=",
        )
        self.assertEqual(status, 401)
        self.assertIn("not valid", payload["error"])

    def test_stale_timestamp(self):
        status, payload = _post(
            {"callRef": "call_1", "tool": "lookup", "arguments": {}, "idempotencyKey": "k1"},
            timestamp=str(int(NOW) - 301),
        )
        self.assertEqual(status, 401)
        self.assertIn("too old", payload["error"])

    def test_replayed_idempotency_key(self):
        calls = {"n": 0}

        def run(_args):
            calls["n"] += 1
            return "once"

        register_tool("lookup", run)
        body = {"callRef": "call_1", "tool": "lookup", "arguments": {}, "idempotencyKey": "same"}
        first, payload = _post(body)
        self.assertEqual(first, 200)
        self.assertEqual(payload["result"], "once")
        status, again = _post(body)
        self.assertEqual(status, 200)
        self.assertEqual(again["result"], "once")
        self.assertEqual(calls["n"], 1)
        other = dict(body, arguments={"n": 1})
        status, payload = _post(other)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], REPLAY_ERROR)

    def test_oversize_argument(self):
        status, payload = _post(
            {
                "callRef": "call_1",
                "tool": "lookup",
                "arguments": {"note": "x" * 1001},
                "idempotencyKey": "k1",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"], "An argument is too long.")


class EndpointSwitchTests(unittest.TestCase):
    def test_fake_keeps_endpoint_and_live_client_drops_it(self):
        self.assertTrue(TOOL_ENDPOINT_ON_MAIN)
        tool = Tool(name="lookup", description="Look up an account.", endpoint=ENDPOINT)
        self.assertEqual(tool.kind, "function")
        body = tool_body(tool)
        self.assertEqual(body["endpoint"], ENDPOINT)
        self.assertNotIn("code", body)
        fake = FakePramaan()
        Service(fake).publish(
            Agent(name="reception", prompt="Answer.", tools=[tool])
        )
        sent = fake.calls[0]
        self.assertEqual(sent["body"]["endpoint"], ENDPOINT)

        seen = {}

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class Capture(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen["body"] = json.loads(self.rfile.read(length).decode())
                data = json.dumps({"name": "lookup"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            PramaanClient("test-key", f"http://127.0.0.1:{port}").call(
                "voice.tool.create", body, idempotency_key="key-1"
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["body"]["endpoint"], ENDPOINT)
        self.assertEqual(seen["body"]["kind"], "function")

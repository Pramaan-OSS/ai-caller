"""A switched-off group answers with a plain sentence. No fallback and no completed row."""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_caller.evaluate import judge_transcript
from ai_caller.model import Agent
from ai_caller.pramaan import (
    NOT_AVAILABLE,
    FakePramaan,
    OutcomeUnavailable,
    PramaanClient,
    PramaanError,
)
from ai_caller.server import make_server
from ai_caller.service import Service


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        exc.close()
        return exc.code, json.loads(raw or "{}")


def _get(base: str, path: str) -> tuple[int, dict, str]:
    try:
        with urllib.request.urlopen(base + path) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw or "{}"), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        exc.close()
        return exc.code, json.loads(raw or "{}"), raw


class ChatShapeTests(unittest.TestCase):
    def test_messages_and_max_tokens_floor(self) -> None:
        fake = FakePramaan()
        with self.assertRaises(PramaanError) as low:
            fake.call(
                "llm.chat",
                {"messages": [{"role": "user", "content": "Hi"}], "maxTokens": 8},
            )
        self.assertEqual(low.exception.code, "invalid_request")
        self.assertIn("64", str(low.exception))
        self.assertEqual(fake.calls, [])
        with self.assertRaises(PramaanError) as prompted:
            fake.call(
                "llm.complete",
                {
                    "prompt": "Hi",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "maxTokens": 64,
                },
            )
        self.assertIn("messages", str(prompted.exception))
        self.assertNotIsInstance(prompted.exception, OutcomeUnavailable)
        self.assertEqual(fake.calls, [])

        service = Service(fake)
        service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        summary = next(item for item in fake.calls if item["id"] == "llm.chat")
        self.assertNotIn("prompt", summary["body"])
        self.assertIsInstance(summary["body"]["messages"], list)
        self.assertEqual(summary["body"]["maxTokens"], 256)

        judged = judge_transcript(fake, "Hello.", "Did the caller get a greeting?")
        self.assertIn("passed", judged)
        judgement = [item for item in fake.calls if item["id"] == "llm.chat"][-1]
        self.assertEqual(judgement["body"]["maxTokens"], 256)
        self.assertNotIn("prompt", judgement["body"])


class NotAvailableTests(unittest.TestCase):
    def test_platform_message_is_not_a_fallback(self) -> None:
        class Off(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                raw = json.dumps(
                    {"error": {"code": "upstream_unavailable", "message": "not available yet"}}
                ).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Off)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(PramaanError) as caught:
                client.call("voice.call.start-agent", {"to": "+14155550101"}, idempotency_key="off-1")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertEqual(str(caught.exception), NOT_AVAILABLE)
        self.assertNotIsInstance(caught.exception, OutcomeUnavailable)
        self.assertNotIn("Traceback", str(caught.exception))

    def test_fake_groups_do_not_succeed(self) -> None:
        fake = FakePramaan()
        fake.live_off = True
        service = Service(fake)
        with self.assertRaises(PramaanError) as published:
            service.publish(
                Agent(name="reception", prompt="Answer.", greeting="Hello.", language="en")
            )
        self.assertEqual(str(published.exception), NOT_AVAILABLE)
        self.assertEqual(service.list_agents(), [])
        for capability_id in (
            "phone.numbers.search",
            "voice.knowledge-bases.list",
            "audio.voices.list",
            "calendar.event.create",
            "law.matter.create",
            "mailbox.list",
            "email.inbox.list",
            "domains.registration.check",
            "document.convert",
            "signature.digest.sign",
            "signature.timestamp.create",
            "identity.verification.start",
        ):
            with self.assertRaises(PramaanError) as off:
                fake.call(capability_id, {}, idempotency_key="live-off")
            self.assertEqual(str(off.exception), NOT_AVAILABLE)
        with self.assertRaises(PramaanError) as send:
            fake.call("email.send", {"to": "ada@example.com"}, idempotency_key="live-off")
        self.assertNotEqual(str(send.exception), NOT_AVAILABLE)
        with self.assertRaises(PramaanError) as estimate:
            service.estimate_agent_call(
                to="+14155550101", from_number="+14155550100", assistant="reception"
            )
        self.assertEqual(str(estimate.exception), NOT_AVAILABLE)
        self.assertNotIsInstance(estimate.exception, OutcomeUnavailable)
        with self.assertRaises(PramaanError):
            service.place_agent_call(
                to="+14155550102",
                from_number="+14155550100",
                assistant="reception",
                plan="plan_off",
                outcome_body={
                    "input": {
                        "to": "+14155550102",
                        "from": "+14155550100",
                        "assistant": "reception",
                        "variables": {},
                        "maxDurationSeconds": 120,
                        "recording": False,
                    },
                    "options": {},
                    "basis": {"ground": "legitimate_interest"},
                },
            )
        saved = service.list_calls()
        self.assertTrue(saved)
        self.assertTrue(all(row["status"] != "completed" for row in saved))
        failed = next(row for row in saved if row["to"] == "+14155550102")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failureReason"], NOT_AVAILABLE)
        self.assertTrue(failed["retryable"])
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.call.start-agent"],
            [],
        )

    def test_screens_keep_working(self) -> None:
        os.environ["PRAMAAN_FAKE"] = "1"
        os.environ["PRAMAAN_LIVE_OFF"] = "1"
        os.environ.pop("PRAMAAN_API_KEY", None)
        server = make_server(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for path in ("/api/knowledge", "/api/numbers", "/api/campaigns"):
                status, payload, raw = _get(base, path)
                self.assertEqual(status, 400, path)
                self.assertEqual(payload, {"error": NOT_AVAILABLE})
                self.assertNotIn("Traceback", raw)
                self.assertNotIn("livekit", raw.lower())
            status, agents, _raw = _get(base, "/api/agents")
            self.assertEqual(status, 200)
            self.assertEqual(agents["items"], [])
            status, settings, _raw = _get(base, "/api/settings")
            self.assertEqual(status, 200)
            self.assertIn("callSeconds", settings["limits"])
            status, added = _post(base, "/api/opt-outs", {"address": "+14155550101"})
            self.assertEqual(status, 200)
            self.assertNotIn("error", added)
            status, opted, _raw = _get(base, "/api/opt-outs")
            self.assertEqual(status, 200)
            self.assertEqual(opted["items"][0]["address"], "+14155550101")
            status, estimate = _post(
                base,
                "/api/calls/estimate",
                {
                    "assistant": "reception",
                    "from": "+14155550100",
                    "to": "+14155550109",
                    "maxDurationSeconds": 120,
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(estimate, {"error": NOT_AVAILABLE})
            status, created = _post(
                base,
                "/api/agents",
                {
                    "name": "reception",
                    "prompt": "Answer.",
                    "greeting": "Hello.",
                    "language": "en",
                    "maxDurationSeconds": 120,
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(created["error"], NOT_AVAILABLE)
            status, agents, _raw = _get(base, "/api/agents")
            self.assertEqual(agents["items"], [])
        finally:
            os.environ.pop("PRAMAAN_LIVE_OFF", None)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

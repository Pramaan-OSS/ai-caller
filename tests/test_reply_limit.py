"""An empty reply that hit maxTokens is a configuration error. It is not retried."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_caller.pramaan import FakePramaan, PramaanClient, PramaanError, ReplyLimit
from ai_caller.service import Service


class ReplyLimitTests(unittest.TestCase):
    def test_http_400_naming_max_tokens_is_plain_and_once(self) -> None:
        seen = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                seen["n"] += 1
                raw = json.dumps(
                    {
                        "error": {
                            "code": "invalid_request",
                            "message": "maxTokens was reached before any text was produced",
                        }
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(ReplyLimit) as caught:
                client.call(
                    "llm.complete",
                    {"messages": [{"role": "user", "content": "Say hello."}], "maxTokens": 256},
                    idempotency_key="limit-1",
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["n"], 1)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(
            str(caught.exception),
            "The reply limit is too small for an answer. Raise it and try again.",
        )
        self.assertNotIn("maxTokens", str(caught.exception))

    def test_other_invalid_request_stays_generic(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                raw = json.dumps(
                    {"error": {"code": "invalid_request", "message": "Missing a required field."}}
                ).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(PramaanError) as caught:
                client.call(
                    "llm.chat",
                    {"messages": [{"role": "user", "content": "Hi"}], "maxTokens": 256},
                    idempotency_key="other-400",
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotIsInstance(caught.exception, ReplyLimit)
        self.assertEqual(str(caught.exception), "The request failed.")

    def test_summary_does_not_retry_the_same_limit(self) -> None:
        fake = FakePramaan()
        fake.model_replies = ["limit"]
        service = Service(fake)
        with self.assertRaises(ReplyLimit):
            service.place_agent_call(
                to="+14155550101", from_number="+14155550100", assistant="reception"
            )
        chats = [item for item in fake.calls if item["id"] == "llm.chat"]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["body"]["maxTokens"], 256)
        saved = next(row for row in service.list_calls() if row["to"] == "+14155550101")
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["retryable"])
        self.assertEqual(
            saved["failureReason"],
            "The reply limit is too small for an answer. Raise it and try again.",
        )
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.call.start-agent"],
            ["voice.call.start-agent"],
        )


if __name__ == "__main__":
    unittest.main()

"""A reply that stops at the limit is not a finished result."""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from ai_caller.pramaan import FakePramaan, ReplyCutOff, ReplyLimit, read_model_reply
from ai_caller.server import make_server
from ai_caller.service import Service
from ai_caller.store import Store


CUT = "This summary was cut off. Try again."


def _place(service: Service, number: str = "+14155550102") -> dict:
    return service.place_agent_call(
        to=number, from_number="+14155550100", assistant="reception"
    )


def _chats(fake: FakePramaan) -> list:
    return [item for item in fake.calls if item["id"] == "llm.chat"]


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        error.close()
        return error.code, json.loads(raw) if raw else {}


def _get(base: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read().decode())


class ReplyCutTests(unittest.TestCase):
    def test_free_text_continues_once(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [
            {"text": "The caller was", "finishReason": "length"},
            {"text": " greeted.", "finishReason": "stop"},
        ]
        service = Service(fake)
        saved = _place(service)
        self.assertEqual(saved["status"], "completed")
        self.assertFalse(saved["summaryIncomplete"])
        self.assertEqual(saved["summary"], "The caller was greeted.")
        chats = _chats(fake)
        self.assertEqual(len(chats), 2)
        self.assertTrue(all(item["body"]["maxTokens"] == 256 for item in chats))
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.call.start-agent"],
            ["voice.call.start-agent"],
        )
        again = service.retry_summary(saved["ref"])
        self.assertEqual(again["summary"], "The caller was greeted.")
        self.assertFalse(again["summaryIncomplete"])
        self.assertEqual(len(_chats(fake)), 2)

    def test_free_text_still_cut_is_incomplete(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [
            {"text": "still talking", "finishReason": "length"},
            {"text": " and more", "finishReason": "length"},
        ]
        service = Service(fake)
        saved = _place(service)
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["summaryIncomplete"])
        self.assertEqual(saved["summary"], CUT)
        self.assertNotIn("still talking", saved["summary"])
        self.assertEqual(len(_chats(fake)), 2)
        fake.model_replies = [{"text": "The caller was greeted.", "finishReason": "stop"}]
        again = service.retry_summary(saved["ref"])
        self.assertEqual(again["summary"], "The caller was greeted.")
        self.assertFalse(again["summaryIncomplete"])
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.call.start-agent"],
            ["voice.call.start-agent"],
        )
        row = next(item for item in service.list_calls() if item["to"] == "+14155550102")
        self.assertEqual(row["status"], "completed")
        self.assertFalse(row["summaryIncomplete"])

    def test_empty_length_does_not_continue(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [{"text": "", "finishReason": "length"}]
        service = Service(fake)
        saved = _place(service)
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["summaryIncomplete"])
        self.assertEqual(saved["summary"], CUT)
        self.assertEqual(len(_chats(fake)), 1)

    def test_follow_up_limit_is_not_retried(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [
            {"text": "The caller was", "finishReason": "length"},
            "limit",
        ]
        service = Service(fake)
        with self.assertRaises(ReplyLimit):
            _place(service)
        self.assertEqual(len(_chats(fake)), 2)
        saved = next(row for row in service.list_calls() if row["to"] == "+14155550102")
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["retryable"])
        self.assertNotIn("The caller was", saved.get("failureReason") or "")
        self.assertNotIn("The caller was", saved.get("summary") or "")

    def test_structured_length_is_not_parsed(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [
            {"text": '{"passed": true', "finishReason": "length"},
        ]
        service = Service(fake)
        with self.assertRaises(ReplyCutOff) as caught:
            service.judge("Hello.", "Did the caller get a greeting?")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "This reply was cut off. Try again.")
        self.assertNotIn("passed", str(caught.exception))
        self.assertEqual(len(_chats(fake)), 1)
        self.assertEqual(_chats(fake)[0]["body"]["maxTokens"], 256)

    def test_tool_call_length_is_not_parsed(self) -> None:
        with self.assertRaises(ReplyCutOff) as caught:
            read_model_reply(
                {
                    "text": '{"passed": true',
                    "finishReason": "length",
                    "toolCalls": [{"name": "lookup"}],
                },
                structured=False,
            )
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("passed", str(caught.exception))

    def test_complete_length_is_not_parsed(self) -> None:
        fake = FakePramaan()
        fake.model_replies = [
            {"text": '{"passed": true', "finishReason": "length"},
        ]
        payload = fake.call(
            "llm.complete",
            {"messages": [{"role": "user", "content": "Say hello."}], "maxTokens": 256},
            idempotency_key="complete-1",
        )
        with self.assertRaises(ReplyCutOff):
            read_model_reply(payload, structured=True)
        completes = [item for item in fake.calls if item["id"] == "llm.complete"]
        self.assertEqual(len(completes), 1)

    def test_summary_route_stays_completed(self) -> None:
        os.environ["PRAMAAN_FAKE"] = "1"
        os.environ["PRAMAAN_SUMMARY_CUT"] = "1"
        os.environ.pop("PRAMAAN_API_KEY", None)
        os.environ.pop("PRAMAAN_CALL_OPEN", None)
        server = make_server(port=0, store=Store())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            status, missing = _post(base, "/api/calls/missing/summary", {})
            self.assertEqual(status, 404)
            self.assertEqual(missing.get("error"), "Not found.")
            status, placed = _post(
                base,
                "/api/calls",
                {
                    "confirm": True,
                    "to": "+14155550102",
                    "from": "+14155550100",
                    "assistant": "reception",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(placed["status"], "completed")
            self.assertTrue(placed["summaryIncomplete"])
            self.assertEqual(placed["summary"], CUT)
            ref = placed["ref"]
            status, again = _post(base, f"/api/calls/{ref}/summary", {})
            self.assertEqual(status, 200)
            self.assertTrue(again["summaryIncomplete"])
            self.assertEqual(again["summary"], CUT)
            status, listed = _get(base, "/api/calls")
            self.assertEqual(status, 200)
            row = next(item for item in listed["items"] if item["ref"] == ref)
            self.assertEqual(row["status"], "completed")
            self.assertTrue(row["summaryIncomplete"])
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("PRAMAAN_SUMMARY_CUT", None)


if __name__ == "__main__":
    unittest.main()

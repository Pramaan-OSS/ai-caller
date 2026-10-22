import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_caller.evaluate import judge_transcript
from ai_caller.model import Agent, DefinitionError, Tool
from ai_caller.pramaan import FakePramaan, PramaanClient, PramaanError, Unsupported
from ai_caller.server import make_server
from ai_caller.service import Service
from ai_caller.store import Store
from ai_caller.workflows import handoff, warm_transfer_to_person


class CompileTests(unittest.TestCase):
    def test_publish_order_and_limits(self):
        fake = FakePramaan()
        service = Service(fake)
        agent = Agent(
            name="reception",
            prompt="Answer the phone.",
            greeting="Hello.",
            tools=[Tool(name="finish", description="End the call.", kind="end_call")],
            knowledge_text="The office opens at nine.",
        )
        service.publish(agent)
        ids = [item["id"] for item in fake.calls]
        self.assertEqual(
            ids,
            ["voice.tool.create", "voice.knowledge-base.create", "voice.assistant.create"],
        )
        self.assertTrue(all(item["idempotency_key"] for item in fake.calls))
        created = next(iter(fake.knowledge.values()))
        assistant = next(item for item in fake.calls if item["id"] == "voice.assistant.create")
        self.assertEqual(assistant["body"]["knowledgeBaseRef"], created["ref"])
        self.assertNotIn("voiceRef", assistant["body"])
        self.assertNotIn("text", created)
        coded = Tool(name="lookup", description="Look up an account.", code="return 1")
        self.assertEqual(coded.kind, "code")
        body = __import__("ai_caller.compile", fromlist=["tool_body"]).tool_body(coded)
        self.assertEqual(body["kind"], "code")
        self.assertEqual(body["code"], "return 1")
        self.assertNotIn("url", body)
        self.assertNotIn("voice", assistant["body"])
        with self.assertRaises(DefinitionError):
            Agent(name="reception", prompt="Hi", max_duration_seconds=601)

    def test_listed_voice_is_sent_and_an_unlisted_voice_is_kept(self):
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone.", voice="female-1"))
        created = next(item for item in fake.calls if item["id"] == "voice.assistant.create")
        self.assertEqual(created["body"]["voice"], "female-1")
        self.assertNotIn("voiceRef", created["body"])
        self.assertEqual(service.store.get_agent("reception")["voice"], "female-1")
        service.update_agent(Agent(name="reception", prompt="Answer the phone.", voice="retired"))
        updated = next(item for item in fake.calls if item["id"] == "voice.assistant.update")
        self.assertNotIn("voice", updated["body"]["patch"])
        self.assertEqual(service.store.get_agent("reception")["voice"], "retired")
        service.update_agent(Agent(name="reception", prompt="Answer the phone.", voice="male-2"))
        sent = [item for item in fake.calls if item["id"] == "voice.assistant.update"][-1]
        self.assertEqual(sent["body"]["patch"]["voice"], "male-2")
        with self.assertRaises(PramaanError) as caught:
            fake.call(
                "voice.assistant.create",
                {"name": "other", "prompt": "Hi", "voice": "nope"},
                idempotency_key="voice-nope",
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_caller_number_is_off_until_the_platform_allows_it(self):
        fake = FakePramaan()
        service = Service(fake)
        with self.assertRaises(PramaanError) as off:
            service.import_caller_number("+14155550198", confirm=True)
        self.assertEqual(off.exception.code, "upstream_unavailable")
        self.assertEqual(str(off.exception), "This is not available yet.")
        self.assertEqual(service.list_caller_numbers(), [])
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.number.import"],
            ["voice.number.import"],
        )
        with self.assertRaises(PramaanError) as disconnected:
            service.disconnect_caller_number("+14155550198", confirm=True)
        self.assertEqual(str(disconnected.exception), "This is not available yet.")
        self.assertNotIn("voice.call.start-agent", [item["id"] for item in fake.calls])

    def test_imported_caller_number_can_place_calls(self):
        fake = FakePramaan()
        fake.caller_numbers = True
        service = Service(fake)
        with self.assertRaises(ValueError):
            service.import_caller_number("+14155550198", confirm=False)
        imported = service.import_caller_number("+14155550198", name="Front desk", confirm=True)
        self.assertEqual(imported["outboundEnabled"], True)
        self.assertEqual(imported["type"], "telephone")
        self.assertEqual(service.list_caller_numbers()[0]["number"], "+14155550198")
        with self.assertRaises(PramaanError) as again:
            service.import_caller_number("+14155550198", confirm=True)
        self.assertEqual(again.exception.code, "conflict")
        placed = service.place_agent_call(
            to="+14155550101", from_number="+14155550198", assistant="reception"
        )
        dial = next(item for item in fake.calls if item["id"] == "voice.call.start-agent" and not item.get("via"))
        self.assertEqual(dial["body"]["from"], "+14155550198")
        self.assertEqual(placed["from"], "+14155550198")
        started = service.start_agent_campaign(
            "Notice", "+14155550198", "reception", [{"id": "a", "to": "+14155550102"}]
        )
        bulk = next(item for item in fake.calls if item["id"] == "voice.call.start-agent-bulk")
        self.assertEqual(bulk["body"]["rows"][0]["from"], "+14155550198")
        self.assertEqual(started["path"], "agent")
        gone = service.disconnect_caller_number("+14155550198", confirm=True)
        self.assertEqual(gone["disconnected"], True)
        self.assertEqual(service.list_caller_numbers(), [])
        with self.assertRaises(PramaanError) as missing:
            service.disconnect_caller_number("+14155550198", confirm=True)
        self.assertEqual(missing.exception.code, "not_found")

    def test_opt_out_blocks_the_call(self):
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone."))
        service.opt_out("+14155550101")
        from ai_caller.pramaan import OptOut

        with self.assertRaises(OptOut):
            service.place_agent_call(
                to="+14155550101", from_number="+14155550100", assistant="reception"
            )
        self.assertNotIn("voice.call.start-agent", [item["id"] for item in fake.calls])

    def test_number_list_uses_results_and_hides_price(self):
        fake = FakePramaan()
        service = Service(fake)
        found = service.search_numbers("US", "415")
        self.assertNotIn("monthlyPriceUsd", found)
        service.buy_number("US")
        numbers = service.list_numbers()
        self.assertEqual(numbers["items"][0]["phoneNumber"], "+14155550100")
        self.assertEqual(numbers["includedNote"], "")
        bound = service.attach_inbound("+14155550199", assistant="reception")
        self.assertEqual(bound["assistant"], "reception")
        sent = [item for item in fake.calls if item["id"] == "voice.number.update"][-1]
        self.assertEqual(sent["body"]["patch"], {"assistant": "reception"})
        self.assertNotIn("voice.call.start-configured", [item["id"] for item in fake.calls])

    def test_purchased_numbers_stay_listed_when_included_numbers_are_off(self):
        """The rented list is live while the included-numbers list is switched off."""
        from ai_caller.pramaan import NOT_AVAILABLE, PramaanError

        fake = FakePramaan()
        service = Service(fake)
        service.buy_number("US")

        class HalfOff:
            def call(self, capability_id: str, body=None, *, idempotency_key=None):
                if capability_id == "voice.numbers.list":
                    raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
                return fake.call(capability_id, body, idempotency_key=idempotency_key)

        listed = Service(HalfOff()).list_numbers()
        self.assertEqual(listed["items"][0]["phoneNumber"], "+14155550100")
        self.assertEqual(listed["includedNote"], NOT_AVAILABLE)

    def test_place_call_and_judge(self):
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone."))
        result = service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("voice.call.start-agent", [item["id"] for item in fake.calls])
        judgement = judge_transcript(fake, result["transcript"], "Did the agent greet the caller?")
        self.assertTrue(judgement["passed"])

    def test_handoff_and_blocked_warm_transfer(self):
        fake = FakePramaan()
        service = Service(fake)
        handoff(
            service,
            Agent(name="desk", prompt="Greet the caller."),
            Agent(name="billing", prompt="Handle a bill."),
            "front",
        )
        self.assertIn("front", fake.squads)
        self.assertEqual(fake.squads["front"]["members"][0]["assistant"], "desk")
        with self.assertRaises(Unsupported):
            warm_transfer_to_person("+14155550123")
        blocked = "twi" + "lio"
        self.assertFalse(any(blocked in item["id"] for item in fake.calls))

    def test_campaign_cap(self):
        service = Service(FakePramaan())
        with self.assertRaises(ValueError):
            service.start_campaign(
                "Notice",
                "+14155550100",
                "Hello.",
                [{"id": str(i), "to": "+14155550101"} for i in range(101)],
            )


class BulkAndKnowledgeTests(unittest.TestCase):
    def test_bulk_rows_settle_and_a_miss_is_free(self):
        fake = FakePramaan()
        result = fake.call(
            "voice.call.start-agent-bulk",
            {
                "rows": [
                    {"id": "one", "to": "+14155550101", "from": "+14155550100", "assistant": "reception"},
                    {"id": "away", "to": "+14155550102", "from": "+14155550100", "assistant": "reception"},
                    {
                        "id": "later",
                        "to": "+14155550103",
                        "from": "+14155550100",
                        "assistant": "reception",
                        "scheduleAt": "2099-01-01T00:00:00Z",
                    },
                    {"id": "crew", "to": "+14155550104", "from": "+14155550100", "squad": "front"},
                ]
            },
            idempotency_key="bulk-1",
        )
        by_id = {row["id"]: row for row in result["results"]}
        self.assertEqual([row["id"] for row in result["results"]], ["one", "away", "later", "crew"])
        self.assertTrue(by_id["one"]["found"])
        self.assertGreater(by_id["one"]["costMicro"], 0)
        self.assertLessEqual(len(by_id["one"]["transcript"]), 10000)
        self.assertEqual(by_id["away"]["status"], "no_answer")
        self.assertEqual(by_id["away"]["costMicro"], 0)
        self.assertEqual(by_id["later"]["error"]["code"], "invalid_request")
        self.assertEqual(by_id["later"]["costMicro"], 0)
        self.assertEqual(by_id["crew"]["costMicro"], 0)
        self.assertEqual(result["costMicro"], by_id["one"]["costMicro"])
        self.assertEqual(result["summary"]["total"], 4)

    def test_knowledge_list_and_get_hide_the_text(self):
        fake = FakePramaan()
        created = fake.call(
            "voice.knowledge-base.create",
            {"name": "hours", "text": "The office opens at nine."},
            idempotency_key="kb-1",
        )
        listed = fake.call("voice.knowledge-bases.list", {"limit": 25})
        self.assertEqual(listed["results"][0]["ref"], created["ref"])
        detail = fake.call("voice.knowledge-base.get", {"ref": created["ref"]})
        self.assertEqual(detail["status"], "ready")
        self.assertNotIn("text", detail)
        self.assertNotIn("nine", str(detail))

    def test_deleting_a_knowledge_base_confirms_and_stays_when_unavailable(self):
        fake = FakePramaan()
        service = Service(fake)
        created = service.create_knowledge("hours", "The office opens at nine.")
        service.publish(
            Agent(
                name="reception",
                prompt="Answer.",
                greeting="Hello.",
                knowledge_base_ref=created["ref"],
            )
        )
        with self.assertRaises(ValueError):
            service.delete_knowledge(created["ref"], confirm=False)
        self.assertEqual(service.list_knowledge()[0]["ref"], created["ref"])
        fake.live_off = True
        with self.assertRaises(PramaanError) as off:
            service.delete_knowledge(created["ref"], confirm=True)
        self.assertEqual(str(off.exception), "This is not available yet.")
        self.assertEqual(service.list_knowledge()[0]["ref"], created["ref"])
        self.assertIn(created["ref"], fake.knowledge)
        self.assertEqual(
            [item["id"] for item in fake.calls if item["id"] == "voice.knowledge-base.delete"],
            ["voice.knowledge-base.delete"],
        )
        fake.live_off = False
        deleted = service.delete_knowledge(created["ref"], confirm=True)
        self.assertEqual(deleted, {"deleted": True})
        sent = [item for item in fake.calls if item["id"] == "voice.knowledge-base.delete"][-1]
        self.assertEqual(sent["body"], {"ref": created["ref"], "confirm": True})
        self.assertTrue(sent["idempotency_key"])
        self.assertEqual(service.list_knowledge(), [])
        self.assertNotIn("knowledgeBaseRef", service.list_agents()[0])
        self.assertEqual(fake.knowledge, {})
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        path = handle.name
        handle.close()
        try:
            other = Store(path, "other")
            other.save_knowledge(
                {"ref": "kb_other", "name": "theirs", "status": "ready", "sourceCount": 1}
            )
            other.close()
            local_store = Store(path, "local")
            local = Service(FakePramaan(), local_store)
            try:
                with self.assertRaises(KeyError):
                    local.delete_knowledge("kb_other", confirm=True)
                self.assertFalse(
                    [item for item in local.client.calls if item["id"] == "voice.knowledge-base.delete"]
                )
                check = Store(path, "other")
                try:
                    self.assertEqual(check.get_knowledge("kb_other")["name"], "theirs")
                finally:
                    check.close()
            finally:
                local_store.close()
        finally:
            os.unlink(path)

    def test_bulk_opt_out_skips_one_row(self):
        fake = FakePramaan()
        service = Service(fake)
        service.opt_out("+14155550101")
        started = service.start_agent_campaign(
            "Notice",
            "+14155550100",
            "reception",
            [
                {"id": "blocked", "to": "+14155550101"},
                {"id": "allowed", "to": "+14155550102"},
            ],
        )
        bulk = [item for item in fake.calls if item["id"] == "voice.call.start-agent-bulk"]
        self.assertEqual([row["id"] for row in bulk[0]["body"]["rows"]], ["allowed"])
        by_id = {row["id"]: row for row in started["rows"]}
        self.assertEqual(by_id["blocked"]["error"]["code"], "recipient_opted_out")
        self.assertEqual(by_id["blocked"]["costMicro"], 0)
        self.assertTrue(by_id["allowed"]["found"])
        self.assertNotIn("voice.campaign.create", [item["id"] for item in fake.calls])

    def test_bulk_all_opted_out_does_not_dial(self):
        fake = FakePramaan()
        service = Service(fake)
        service.opt_out("+14155550101")
        started = service.start_agent_campaign(
            "Notice", "+14155550100", "reception", [{"id": "blocked", "to": "+14155550101"}]
        )
        self.assertNotIn("voice.call.start-agent-bulk", [item["id"] for item in fake.calls])
        self.assertEqual(started["rows"][0]["costMicro"], 0)
        self.assertEqual(started["costMicro"], 0)


class HttpClientTests(unittest.TestCase):
    def test_polls_a_job_and_sends_the_key(self):
        seen = {}

        class JobHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["key"] = self.headers.get("Idempotency-Key")
                seen["path"] = self.path
                length = int(self.headers.get("Content-Length") or 0)
                seen["body"] = json.loads(self.rfile.read(length).decode())
                raw = json.dumps({"job_id": "job_1", "status": "queued", "capability": "voice.call.start-agent"}).encode()
                self.send_response(202)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                raw = json.dumps(
                    {
                        "job_id": "job_1",
                        "status": "done",
                        "capability": "voice.call.start-agent",
                        "result": {"status": "completed", "ref": "call_1"},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), JobHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{port}")
            result = client.call(
                "voice.call.start-agent",
                {"to": "+14155550101", "from": "+14155550100", "assistant": "reception"},
                idempotency_key="same-key",
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["ref"], "call_1")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["key"], "same-key")
        self.assertEqual(seen["path"], "/v1/capabilities/voice.call.start-agent")


class ConsoleTests(unittest.TestCase):
    def test_pages_and_place_call(self):
        import os

        os.environ["PRAMAAN_FAKE"] = "1"
        server = make_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("AI Caller", page)
            self.assertNotIn("live" + "kit", page.lower())

            def post(path, body):
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req) as res:
                    return json.loads(res.read().decode())

            post("/api/agents", {"name": "reception", "prompt": "Answer the phone.", "greeting": "Hello."})
            agents = json.loads(urllib.request.urlopen(base + "/api/agents").read().decode())
            self.assertEqual(agents["items"][0]["name"], "reception")
            call = post(
                "/api/calls",
                {
                    "assistant": "reception",
                    "from": "+14155550100",
                    "to": "+14155550101",
                    "confirm": True,
                },
            )
            self.assertEqual(call["status"], "completed")
            transcripts = json.loads(urllib.request.urlopen(base + "/api/transcripts").read().decode())
            self.assertIn("practice transcript", transcripts["items"][0]["transcript"])
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("PRAMAAN_FAKE", None)

    def test_boots_with_a_key_and_does_not_dial(self):
        import os

        os.environ.pop("PRAMAAN_FAKE", None)
        os.environ["PRAMAAN_API_KEY"] = "boot-key"
        server = make_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/").read().decode()
            self.assertIn("AI Caller", page)
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("PRAMAAN_API_KEY", None)


if __name__ == "__main__":
    unittest.main()

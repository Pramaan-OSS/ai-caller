import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from ai_caller.csv_rows import parse_recipients
from ai_caller.model import Agent
from ai_caller.pramaan import FakePramaan, OutcomeCheck, OutcomeLater, OutcomeUnavailable, PramaanError
from ai_caller.server import make_server
from ai_caller.service import Service, silent_wav
from ai_caller.store import Store


END = "we_0123456789abcdef0123456789abcdef"


def _boot():
    os.environ["PRAMAAN_FAKE"] = "1"
    os.environ.pop("PRAMAAN_API_KEY", None)
    server = make_server(port=0, store=Store())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path, body=None, method=None, key=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            content = res.headers.get("Content-Type") or ""
            if "json" in content:
                return res.status, json.loads(raw.decode())
            return res.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        exc.close()
        try:
            payload = json.loads(raw.decode())
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


class CsvTests(unittest.TestCase):
    def test_accepts_two_rows(self):
        rows = parse_recipients("id,to\na,+14155550101\nb,+14155550102\n")
        self.assertEqual(len(rows), 2)

    def test_rejects_a_bad_phone(self):
        with self.assertRaises(ValueError):
            parse_recipients("id,to\na,415\n")

    def test_rejects_a_repeated_id(self):
        with self.assertRaises(ValueError):
            parse_recipients("id,to\na,+14155550101\na,+14155550102\n")

    def test_rejects_101_rows(self):
        lines = ["id,to"] + [f"r{i},+1415555{i:04d}" for i in range(100)]
        # The numbers above are not all valid. Build 101 valid numbers.
        lines = ["id,to"]
        for i in range(101):
            lines.append(f"r{i},+1415555{i:04d}")
        with self.assertRaises(ValueError):
            parse_recipients("\n".join(lines))

    def test_rejects_a_missing_column(self):
        with self.assertRaises(ValueError):
            parse_recipients("name,phone\na,+14155550101\n")


class ServiceOutcomeTests(unittest.TestCase):
    def _service(self):
        return Service(FakePramaan(), Store())

    def test_estimate_does_not_dial(self):
        service = self._service()
        service.publish(Agent(name="reception", prompt="Answer the phone."))
        estimate = service.estimate_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        self.assertFalse(estimate["fallback"])
        self.assertTrue(estimate["plan"])
        self.assertNotIn("voice.call.start-agent", [item["id"] for item in service.client.calls])
        self.assertIn("Nothing is dialed", estimate["sentence"])

    def test_confirmed_plan_uses_the_outcome(self):
        service = self._service()
        estimate = service.estimate_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        placed = service.place_agent_call(
            to="+14155550101",
            from_number="+14155550100",
            assistant="reception",
            plan=estimate["plan"],
            outcome_body=estimate["body"],
        )
        self.assertEqual(placed["outcome"], "voice.agent.call")
        self.assertEqual(placed["summary"], "The caller was greeted.")
        self.assertEqual(service.list_calls()[0]["ref"], placed["ref"])

    def test_scheduled_call_uses_the_capability(self):
        service = self._service()
        estimate = service.estimate_agent_call(
            to="+14155550101",
            from_number="+14155550100",
            assistant="reception",
            schedule_at="2026-10-13T15:00:00Z",
        )
        self.assertTrue(estimate["fallback"])
        self.assertEqual(estimate["capability"], "voice.call.start-agent")

    def test_unavailable_outcome_falls_back(self):
        service = self._service()
        service.client.outcome_state = "down"
        estimate = service.estimate_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        self.assertTrue(estimate["fallback"])
        placed = service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception", plan="plan_x", outcome_body={}
        )
        self.assertEqual(placed["outcome"], "voice.call.start-agent")

    def test_later_is_not_a_fallback(self):
        service = self._service()
        service.client.outcome_state = "later"
        with self.assertRaises(OutcomeLater):
            service.estimate_agent_call(
                to="+14155550101", from_number="+14155550100", assistant="reception"
            )

    def test_long_run_failures_do_not_dial_or_buy(self):
        service = self._service()
        estimate = service.estimate_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        number = service.estimate_number_setup("US", "local", "+14155550100")
        for state, error, code in (
            ("later", OutcomeLater, "rate_limited"),
            ("unknown", OutcomeCheck, "effect_unknown"),
            ("timeout", PramaanError, "upstream_timeout"),
        ):
            service.client.outcome_state = state
            before_runs = len([item for item in service.client.calls if item["id"] == "outcome.run"])
            with self.assertRaises(error) as placed:
                service.place_agent_call(
                    to="+14155550101",
                    from_number="+14155550100",
                    assistant="reception",
                    plan=estimate["plan"],
                    outcome_body=estimate["body"],
                )
            with self.assertRaises(error) as bought:
                service.confirm_number_setup(number)
            self.assertEqual(placed.exception.code, code)
            self.assertEqual(bought.exception.code, code)
            self.assertNotIsInstance(placed.exception, OutcomeUnavailable)
            runs = [item for item in service.client.calls if item["id"] == "outcome.run"]
            self.assertEqual(len(runs), before_runs + 2)
        dials = [
            item
            for item in service.client.calls
            if item["id"] == "voice.call.start-agent" and not item.get("via")
        ]
        self.assertEqual(dials, [])
        self.assertNotIn("phone.number.purchase", [item["id"] for item in service.client.calls])

    def test_hidden_outcome_falls_back_on_estimate(self):
        service = self._service()
        service.client.outcome_state = "hidden"
        with self.assertRaises(OutcomeUnavailable):
            service.client.outcomes_run("voice.agent.call", {}, plan="x", idempotency_key="k")

    def test_number_estimate_shows_the_rate_and_waits(self):
        service = self._service()
        estimate = service.estimate_number_setup("US", "local", "+14155550100")
        self.assertIn("1.15", estimate["sentence"])
        self.assertIn("Nothing is bought", estimate["sentence"])
        self.assertNotIn("phone.number.purchase", [item["id"] for item in service.client.calls])
        bought = service.confirm_number_setup(estimate)
        self.assertEqual(bought["number"], "+14155550100")

    def test_opt_out_blocks_a_campaign_row(self):
        service = self._service()
        service.opt_out("+14155550101")
        from ai_caller.pramaan import OptOut

        with self.assertRaises(OptOut):
            service.start_campaign(
                "Notice", "+14155550100", "Hello.", [{"id": "a", "to": "+14155550101"}]
            )

    def test_tool_test_rejects_a_long_argument(self):
        service = self._service()
        service.save_tool({"name": "lookup", "description": "Find it.", "code": "return 1"})
        self.assertEqual(service.test_tool("lookup", {"q": "ok"})["result"], "Received.")
        with self.assertRaises(ValueError):
            service.test_tool("lookup", {"q": "x" * 1001})

    def test_recording_is_local_audio(self):
        service = self._service()
        placed = service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        audio = service.recording(placed["ref"])
        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertEqual(audio[:4], silent_wav()[:4])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.server, self.base = _boot()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("PRAMAAN_FAKE", None)

    def test_agent_create_edit_delete(self):
        status, created = _req(
            self.base,
            "/api/agents",
            {"name": "reception", "prompt": "Answer.", "greeting": "Hello.", "language": "en", "maxDurationSeconds": 90},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["name"], "reception")
        status, listed = _req(self.base, "/api/agents")
        self.assertEqual(listed["items"][0]["maxDurationSeconds"], 90)
        status, updated = _req(
            self.base,
            "/api/agents/reception",
            {"prompt": "Answer briefly.", "greeting": "Hello.", "language": "es", "maxDurationSeconds": 60},
            method="PUT",
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["name"], "reception")
        status, removed = _req(self.base, "/api/agents/reception", method="DELETE")
        self.assertEqual(status, 200)
        _status, listed = _req(self.base, "/api/agents")
        self.assertEqual(listed["items"], [])

    def test_unknown_agent_is_not_found(self):
        status, payload = _req(self.base, "/api/agents/missing", method="DELETE")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_tool_save_and_test(self):
        status, saved = _req(
            self.base,
            "/api/tools",
            {"name": "lookup", "description": "Find it.", "endpoint": END, "timeoutSeconds": 8},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["name"], "lookup")
        status, test = _req(self.base, "/api/tools/test", {"name": "lookup", "arguments": {"q": "a"}})
        self.assertEqual(test["result"], "Received.")
        status, removed = _req(self.base, "/api/tools/lookup", method="DELETE")
        self.assertEqual(status, 200)

    def test_squad_membership(self):
        _req(self.base, "/api/agents", {"name": "desk", "prompt": "Greet."})
        _req(self.base, "/api/agents", {"name": "billing", "prompt": "Bills."})
        status, squad = _req(
            self.base,
            "/api/squads",
            {"name": "front", "members": [{"assistant": "desk", "handoffTo": ["billing"]}]},
        )
        self.assertEqual(status, 200)
        _status, listed = _req(self.base, "/api/squads")
        self.assertEqual(listed["items"][0]["name"], "front")

    def test_call_needs_confirmation_then_places(self):
        status, refused = _req(
            self.base,
            "/api/calls",
            {"assistant": "reception", "from": "+14155550100", "to": "+14155550101"},
        )
        self.assertEqual(status, 400)
        status, estimate = _req(
            self.base,
            "/api/calls/estimate",
            {"assistant": "reception", "from": "+14155550100", "to": "+14155550101", "maxDurationSeconds": 120},
        )
        self.assertEqual(status, 200)
        self.assertIn("Nothing is dialed", estimate["sentence"])
        status, placed = _req(
            self.base,
            "/api/calls",
            {
                "assistant": "reception",
                "from": "+14155550100",
                "to": "+14155550101",
                "confirm": True,
                "plan": estimate["plan"],
                "outcomeBody": estimate["body"],
            },
            key="call-1",
        )
        self.assertEqual(placed["status"], "completed")
        status, again = _req(
            self.base,
            "/api/calls",
            {
                "assistant": "reception",
                "from": "+14155550100",
                "to": "+14155550101",
                "confirm": True,
                "plan": estimate["plan"],
                "outcomeBody": estimate["body"],
            },
            key="call-1",
        )
        self.assertEqual(again["ref"], placed["ref"])
        status, detail = _req(self.base, "/api/calls/" + placed["ref"])
        self.assertEqual(detail["transcript"], placed["transcript"])
        self.assertTrue(detail["timeline"])
        self.assertTrue(detail["legs"])
        status, audio = _req(self.base, "/api/calls/" + placed["ref"] + "/recording")
        self.assertEqual(status, 200)
        self.assertTrue(audio.startswith(b"RIFF"))
        status, ended = _req(self.base, "/api/calls/" + placed["ref"] + "/end", {"confirm": True})
        self.assertEqual(ended["status"], "cancelled")
        status, filtered = _req(self.base, "/api/calls?status=cancelled&assistant=reception")
        self.assertEqual(filtered["items"][0]["ref"], placed["ref"])

    def test_other_call_is_hidden(self):
        status, missing = _req(self.base, "/api/calls/call_missing")
        self.assertEqual(status, 404)

    def test_purchase_needs_confirmation(self):
        status, refused = _req(self.base, "/api/numbers/purchase", {"country": "US"})
        self.assertEqual(status, 400)
        status, estimate = _req(
            self.base, "/api/numbers/estimate", {"country": "US", "phoneNumber": "+14155550100"}
        )
        self.assertIn("1.15", estimate["sentence"])
        status, bought = _req(
            self.base,
            "/api/numbers/purchase",
            {"confirm": True, "estimate": estimate},
        )
        self.assertEqual(bought["number"], "+14155550100")

    def test_search_hides_a_price_field(self):
        status, found = _req(self.base, "/api/numbers/search", {"country": "US", "areaCode": "415"})
        self.assertEqual(status, 200)
        self.assertNotIn("monthlyPriceUsd", json.dumps(found))

    def test_included_number(self):
        status, row = _req(self.base, "/api/numbers/included", {"areaCode": "415"})
        self.assertEqual(row["number"], "+14155550199")

    def test_attach_is_not_yet_live(self):
        status, payload = _req(
            self.base, "/api/numbers/attach", {"number": "+14155550199", "assistant": "reception"}
        )
        self.assertEqual(status, 409)
        self.assertIn("not yet live", payload["error"])

    def test_campaign_csv_pause_resume_finish(self):
        status, checked = _req(
            self.base, "/api/campaigns/csv", {"csv": "id,to\na,+14155550101\n"}
        )
        self.assertEqual(checked["rows"][0]["to"], "+14155550101")
        status, refused = _req(
            self.base,
            "/api/campaigns",
            {"name": "Notice", "from": "+14155550100", "message": "Ready.", "rows": checked["rows"]},
        )
        self.assertEqual(status, 400)
        status, started = _req(
            self.base,
            "/api/campaigns",
            {
                "name": "Notice",
                "from": "+14155550100",
                "message": "Ready.",
                "rows": checked["rows"],
                "confirm": True,
            },
        )
        self.assertEqual(started["state"], "running")
        ref = started["ref"]
        status, paused = _req(self.base, f"/api/campaigns/{ref}/pause", {})
        self.assertEqual(paused["state"], "paused")
        status, resumed = _req(self.base, f"/api/campaigns/{ref}/resume", {})
        self.assertEqual(resumed["state"], "running")
        status, finished = _req(self.base, f"/api/campaigns/{ref}/finish", {})
        self.assertEqual(finished["state"], "finished")
        status, results = _req(self.base, f"/api/campaigns/{ref}/results")
        self.assertEqual(results["items"][0]["state"], "finished")

    def test_opt_out_blocks_the_call_route(self):
        status, added = _req(self.base, "/api/opt-outs", {"address": "+14155550101"})
        self.assertEqual(status, 200)
        status, blocked = _req(
            self.base,
            "/api/calls",
            {
                "assistant": "reception",
                "from": "+14155550100",
                "to": "+14155550101",
                "confirm": True,
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("do-not-call", blocked["error"])
        status, removed = _req(self.base, "/api/opt-outs/remove", {"address": "+14155550101"})
        self.assertEqual(status, 400)
        status, removed = _req(
            self.base, "/api/opt-outs/remove", {"address": "+14155550101", "confirm": True}
        )
        self.assertEqual(status, 200)

    def test_settings_hide_the_key(self):
        os.environ["PRAMAAN_API_KEY"] = "secret-key-value"
        os.environ["PRAMAAN_WEBHOOK_SECRET"] = "whsec_test"
        try:
            status, view = _req(self.base, "/api/settings")
            self.assertTrue(view["keySet"])
            self.assertTrue(view["secretSet"])
            self.assertNotIn("secret-key-value", json.dumps(view))
            self.assertNotIn("whsec_test", json.dumps(view))
            self.assertIn("one project", view["liveChecks"])
            status, saved = _req(self.base, "/api/settings", {"webhookEndpoint": END})
            self.assertEqual(saved["webhookEndpoint"], END)
            status, bad = _req(self.base, "/api/settings", {"webhookEndpoint": "ep_1"})
            self.assertEqual(status, 400)
        finally:
            os.environ.pop("PRAMAAN_API_KEY", None)
            os.environ.pop("PRAMAAN_WEBHOOK_SECRET", None)

    def test_idempotency_conflict(self):
        body = {"name": "reception", "prompt": "Answer."}
        _req(self.base, "/api/agents", body, key="same")
        status, payload = _req(self.base, "/api/agents", {"name": "other", "prompt": "Answer."}, key="same")
        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_bad_csv_on_the_route(self):
        status, payload = _req(self.base, "/api/campaigns/csv", {"csv": "id,to\na,nope\n"})
        self.assertEqual(status, 400)
        self.assertIn("Row 2", payload["error"])

    def test_duration_and_both_targets_are_refused(self):
        status, payload = _req(
            self.base,
            "/api/calls/estimate",
            {"assistant": "reception", "from": "+14155550100", "to": "+14155550101", "maxDurationSeconds": 9},
        )
        self.assertEqual(status, 400)
        self.assertIn("10", payload["error"])
        status, payload = _req(
            self.base,
            "/api/calls/estimate",
            {
                "assistant": "reception",
                "squad": "front",
                "from": "+14155550100",
                "to": "+14155550101",
            },
        )
        self.assertEqual(status, 400)

    def test_squad_call_and_variables(self):
        service_status, placed = _req(
            self.base,
            "/api/calls",
            {
                "squad": "front",
                "from": "+14155550100",
                "to": "+14155550108",
                "variables": {"name": "Ada"},
                "confirm": True,
            },
        )
        self.assertEqual(service_status, 200)
        self.assertEqual(placed["squad"], "front")
        self.assertEqual(placed["variables"]["name"], "Ada")
        status, found = _req(self.base, "/api/calls?q=50108")
        self.assertEqual(found["items"][0]["ref"], placed["ref"])

    def test_missing_recording_and_tool(self):
        status, _payload = _req(self.base, "/api/calls/call_missing/recording")
        self.assertEqual(status, 404)
        status, _payload = _req(self.base, "/api/tools/missing", method="DELETE")
        self.assertEqual(status, 404)

    def test_empty_csv_and_header_only(self):
        status, _payload = _req(self.base, "/api/campaigns/csv", {"csv": ""})
        self.assertEqual(status, 400)
        status, _payload = _req(self.base, "/api/campaigns/csv", {"csv": "id,to\n"})
        self.assertEqual(status, 400)

    def test_clear_webhook_endpoint(self):
        status, saved = _req(self.base, "/api/settings", {"webhookEndpoint": ""})
        self.assertEqual(status, 200)
        self.assertEqual(saved["webhookEndpoint"], "")

    def test_end_without_confirmation(self):
        status, payload = _req(self.base, "/api/calls/call_missing/end", {})
        self.assertEqual(status, 400)
        self.assertIn("Confirm", payload["error"])

    def test_end_refuses_a_call_that_already_ended(self):
        status, estimate = _req(
            self.base,
            "/api/calls/estimate",
            {"assistant": "reception", "from": "+14155550100", "to": "+14155550101"},
        )
        self.assertEqual(status, 200)
        status, placed = _req(
            self.base,
            "/api/calls",
            {
                "assistant": "reception",
                "from": "+14155550100",
                "to": "+14155550101",
                "confirm": True,
                "plan": estimate["plan"],
                "outcomeBody": estimate["body"],
            },
        )
        self.assertEqual(status, 200)
        status, ended = _req(self.base, "/api/calls/" + placed["ref"] + "/end", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(ended["status"], "cancelled")
        status, again = _req(self.base, "/api/calls/" + placed["ref"] + "/end", {"confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("already ended", again["error"])

    def test_end_does_not_touch_another_owners_call(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        path = handle.name
        handle.close()
        other = Store(path, "other")
        other.save_call({"ref": "call_other", "status": "in_progress", "to": "+14155550101", "from": "+14155550100"})
        other.close()
        local = Store(path, "local")
        os.environ["PRAMAAN_FAKE"] = "1"
        os.environ.pop("PRAMAAN_API_KEY", None)
        server = make_server(port=0, store=local)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            client = server.RequestHandlerClass.service.client
            before = [item for item in client.calls if item["id"] == "voice.call.end"]
            status, payload = _req(base, "/api/calls/call_other/end", {"confirm": True})
            self.assertEqual(status, 404)
            self.assertEqual([item for item in client.calls if item["id"] == "voice.call.end"], before)
            saved = Store(path, "other")
            try:
                self.assertEqual(saved.get_call("call_other")["status"], "in_progress")
            finally:
                saved.close()
        finally:
            server.shutdown()
            server.server_close()
            local.close()
            os.unlink(path)

    def test_knowledge_route_and_agent_attach(self):
        status, refused = _req(self.base, "/api/knowledge", {"name": "hours", "text": "Opens at nine."})
        self.assertEqual(status, 400)
        self.assertIn("Confirm", refused["error"])
        status, created = _req(
            self.base,
            "/api/knowledge",
            {"name": "hours", "text": "Opens at nine.", "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["status"], "ready")
        self.assertNotIn("text", created)
        status, listed = _req(self.base, "/api/knowledge")
        self.assertEqual(listed["items"][0]["ref"], created["ref"])
        status, detail = _req(self.base, "/api/knowledge/" + created["ref"])
        self.assertEqual(detail["sourceCount"], 1)
        self.assertNotIn("nine", json.dumps(detail))
        status, _agent = _req(
            self.base,
            "/api/agents",
            {
                "name": "reception",
                "prompt": "Answer.",
                "greeting": "Hello.",
                "knowledgeBaseRef": created["ref"],
            },
        )
        self.assertEqual(status, 200)
        sent = [
            item
            for item in self.server.RequestHandlerClass.service.client.calls
            if item["id"] == "voice.assistant.create"
        ][-1]
        self.assertEqual(sent["body"]["knowledgeBaseRef"], created["ref"])
        self.assertNotIn("voiceRef", sent["body"])
        self.assertNotIn("voice", sent["body"])
        client = self.server.RequestHandlerClass.service.client
        status, refused_delete = _req(
            self.base, "/api/knowledge/" + created["ref"], {}, method="DELETE"
        )
        self.assertEqual(status, 400)
        self.assertIn("Confirm", refused_delete["error"])
        self.assertFalse([item for item in client.calls if item["id"] == "voice.knowledge-base.delete"])
        client.live_off = True
        try:
            status, off = _req(
                self.base,
                "/api/knowledge/" + created["ref"],
                {"confirm": True},
                method="DELETE",
            )
            self.assertEqual(status, 400)
            self.assertEqual(off["error"], "This is not available yet.")
            status, still = _req(self.base, "/api/knowledge")
            self.assertEqual(still["items"][0]["ref"], created["ref"])
        finally:
            client.live_off = False
        status, deleted = _req(
            self.base,
            "/api/knowledge/" + created["ref"],
            {"confirm": True},
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertEqual(deleted, {"deleted": True})
        removed = [item for item in client.calls if item["id"] == "voice.knowledge-base.delete"][-1]
        self.assertEqual(removed["body"], {"ref": created["ref"], "confirm": True})
        self.assertTrue(removed["idempotency_key"])
        status, empty = _req(self.base, "/api/knowledge")
        self.assertEqual(empty["items"], [])

    def test_caller_number_routes(self):
        client = self.server.RequestHandlerClass.service.client
        status, off = _req(
            self.base,
            "/api/numbers/callers",
            {"number": "+14155550198", "confirm": True},
        )
        self.assertEqual(status, 400)
        self.assertEqual(off["error"], "This is not available yet.")
        self.assertNotIn("voice.call.start-agent", [item["id"] for item in client.calls])
        client.caller_numbers = True
        status, imported = _req(
            self.base,
            "/api/numbers/callers",
            {"number": "+14155550198", "name": "Front desk", "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(imported["number"], "+14155550198")
        status, listed = _req(self.base, "/api/numbers/callers")
        self.assertEqual(listed["items"][0]["number"], "+14155550198")
        status, placed = _req(
            self.base,
            "/api/calls",
            {
                "to": "+14155550101",
                "from": "+14155550198",
                "assistant": "reception",
                "confirm": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(placed["from"], "+14155550198")
        status, started = _req(
            self.base,
            "/api/campaigns",
            {
                "name": "Notice",
                "from": "+14155550198",
                "assistant": "reception",
                "confirm": True,
                "rows": [{"id": "a", "to": "+14155550102"}],
            },
        )
        self.assertEqual(status, 200)
        bulk = [item for item in client.calls if item["id"] == "voice.call.start-agent-bulk"][-1]
        self.assertEqual(bulk["body"]["rows"][0]["from"], "+14155550198")
        status, gone = _req(
            self.base,
            "/api/numbers/callers/disconnect",
            {"number": "+14155550198", "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(gone["disconnected"], True)
        _status, listed = _req(self.base, "/api/numbers/callers")
        self.assertEqual(listed["items"], [])

    def test_agent_voice_round_trip(self):
        status, _saved = _req(
            self.base,
            "/api/agents",
            {"name": "reception", "prompt": "Answer.", "greeting": "Hello.", "voice": "female-1"},
        )
        self.assertEqual(status, 200)
        created = [
            item
            for item in self.server.RequestHandlerClass.service.client.calls
            if item["id"] == "voice.assistant.create"
        ][-1]
        self.assertEqual(created["body"]["voice"], "female-1")
        self.assertNotIn("voiceRef", created["body"])
        status, listed = _req(self.base, "/api/agents")
        self.assertEqual(listed["items"][0]["voice"], "female-1")
        status, _updated = _req(
            self.base,
            "/api/agents/reception",
            {"prompt": "Answer.", "greeting": "Hello.", "voice": "male-1"},
            method="PUT",
        )
        self.assertEqual(status, 200)
        updated = [
            item
            for item in self.server.RequestHandlerClass.service.client.calls
            if item["id"] == "voice.assistant.update"
        ][-1]
        self.assertEqual(updated["body"]["patch"]["voice"], "male-1")

    def test_agent_campaign_route_skips_an_opted_out_row(self):
        status, _added = _req(self.base, "/api/opt-outs", {"address": "+14155550101"})
        self.assertEqual(status, 200)
        status, started = _req(
            self.base,
            "/api/campaigns",
            {
                "name": "Notice",
                "from": "+14155550100",
                "assistant": "reception",
                "confirm": True,
                "rows": [
                    {"id": "blocked", "to": "+14155550101"},
                    {"id": "allowed", "to": "+14155550102"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(started["path"], "agent")
        calls = self.server.RequestHandlerClass.service.client.calls
        bulk = [item for item in calls if item["id"] == "voice.call.start-agent-bulk"]
        self.assertEqual([row["id"] for row in bulk[0]["body"]["rows"]], ["allowed"])
        self.assertNotIn("voice.campaign.create", [item["id"] for item in calls])
        by_id = {row["id"]: row for row in started["rows"]}
        self.assertEqual(by_id["blocked"]["error"]["code"], "recipient_opted_out")
        self.assertEqual(by_id["blocked"]["costMicro"], 0)
        self.assertTrue(by_id["allowed"]["found"])


if __name__ == "__main__":
    unittest.main()

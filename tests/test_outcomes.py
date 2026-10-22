import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_caller.pramaan import (
    CALL_HOLD_MICRO,
    OPEN_RUNS_LATER,
    RUN_LIMIT_SECONDS,
    FakePramaan,
    OutcomeCheck,
    OutcomeLater,
    OutcomeUnavailable,
    PramaanClient,
    PramaanError,
    SETUP_MONTHLY_USD,
)


def _call_body() -> dict:
    return {
        "input": {
            "to": "+14155550101",
            "from": "+14155550100",
            "assistant": "reception",
            "variables": {},
            "maxDurationSeconds": 120,
            "recording": False,
        },
        "options": {},
        "basis": {"ground": "legitimate_interest"},
    }


class FakeOutcomeTests(unittest.TestCase):
    def test_typical_estimates_reach_both_approval_screens(self):
        from unittest.mock import patch
        from ai_caller.model import Agent
        from ai_caller.service import Service
        for typical in (None, 0, 50):
            with self.subTest(typical=typical):
                fake = FakePramaan()
                service = Service(fake)
                service.publish(Agent(name="reception", prompt="Answer the phone."))
                original = fake.outcomes_estimate
                def estimate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if typical is not None:
                        result["price_typical_micro"] = typical
                    return result
                with patch.object(fake, "outcomes_estimate", side_effect=estimate):
                    reviews = [service.estimate_number_setup("US", phone_number="+14155550100"),
                        service.estimate_agent_call(to="+14155550101", from_number="+14155550100", assistant="reception")]
                for review in reviews:
                    self.assertEqual(review.get("priceTypicalMicro"), typical)
                    self.assertIn("priceMaxMicro", review)
                    if typical is None:
                        self.assertNotIn("priceTypicalMicro", review)
                self.assertFalse(any(call["id"] == "outcome.run" for call in fake.calls))

    def test_purchase_refusal_requires_a_second_person_confirmation(self):
        from unittest.mock import patch
        from ai_caller.pramaan import ApprovalRequired
        from ai_caller.service import Service
        fake = FakePramaan()
        service = Service(fake)
        estimate = service.estimate_number_setup("US")
        # The replacement plan is issued by the platform, bound to this body.
        fake._plans["plan_approved"] = fake._plans[estimate["plan"]]
        with patch.object(fake, "outcomes_run", side_effect=ApprovalRequired("plan_approved")) as run:
            review = service.confirm_number_setup(estimate)
            self.assertEqual(run.call_count, 1)
        self.assertTrue(review["approvalRequired"])
        self.assertEqual(review["estimate"]["plan"], "plan_approved")
        self.assertEqual(fake.numbers, [])
        service.confirm_number_setup(review["estimate"])
        self.assertEqual(len(fake.numbers), 1)
        self.assertEqual(fake.calls[-1]["plan"], "plan_approved")

    def test_purchase_plan_expires_and_cannot_change_input(self):
        from unittest.mock import patch
        from ai_caller.service import Service
        for altered in (False, True):
            fake = FakePramaan()
            service = Service(fake)
            with patch("ai_caller.service.time.monotonic", return_value=10):
                estimate = service.estimate_number_setup("US")
            if altered:
                estimate["body"]["input"]["country"] = "GB"
            with patch("ai_caller.service.time.monotonic", return_value=910 if not altered else 11):
                with self.assertRaises(PramaanError):
                    service.confirm_number_setup(estimate)
            self.assertEqual(fake.numbers, [])
            self.assertFalse(any(c["id"] == "outcome.run" for c in fake.calls))

    def test_http_approval_refusal_preserves_plan(self):
        import io
        import urllib.error
        from ai_caller.pramaan import ApprovalRequired
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(
                json.dumps({"error": {"code": "forbidden", "reason": "approval_required", "plan": "plan_new"}}).encode()))
        client = PramaanClient("fake", opener=opener)
        with self.assertRaises(ApprovalRequired) as caught:
            client._request("POST", "/v1/outcomes/phone.number.setup", {}, "k")
        self.assertEqual(caught.exception.plan, "plan_new")

    def test_configuration_deletes_send_required_confirmation(self):
        from ai_caller.service import Service
        from ai_caller.model import Agent
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="retest", prompt="Answer briefly."))
        service.save_tool({"name": "retest", "kind": "code", "code": "return 1"})
        service.delete_tool("retest")
        service.delete_agent("retest")
        for cap in ("voice.tool.delete", "voice.assistant.delete"):
            deleted = next(c for c in fake.calls if c["id"] == cap)
            self.assertTrue(deleted["body"]["confirm"])
            self.assertTrue(deleted["idempotency_key"])
            with self.assertRaises(PramaanError):
                fake.call(cap, {"name": "retest"})

    def test_list_and_get(self):
        fake = FakePramaan()
        listed = fake.outcomes_list()
        ids = [row["id"] for row in listed["results"]]
        self.assertIn("voice.agent.call", ids)
        self.assertIn("phone.number.setup", ids)
        detail = fake.outcomes_get("voice.agent.call")
        self.assertTrue(detail["available"])
        self.assertEqual(detail["price_max_micro"], CALL_HOLD_MICRO)
        self.assertTrue(detail["steps"])

    def test_unknown_outcome(self):
        with self.assertRaises(PramaanError):
            FakePramaan().outcomes_get("missing.outcome")

    def test_estimate_then_run_call(self):
        fake = FakePramaan()
        body = _call_body()
        estimate = fake.outcomes_estimate("voice.agent.call", body)
        self.assertTrue(estimate["plan"])
        self.assertEqual(estimate["price_max_micro"], CALL_HOLD_MICRO)
        self.assertTrue(estimate["requires_approval"])
        report = fake.outcomes_run("voice.agent.call", body, plan=estimate["plan"], idempotency_key="k1")
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["result"]["status"], "completed")
        again = fake.outcomes_run("voice.agent.call", body, plan=estimate["plan"], idempotency_key="k1")
        self.assertEqual(again["result"]["ref"], report["result"]["ref"])

    def test_run_without_plan_is_refused(self):
        fake = FakePramaan()
        with self.assertRaises(PramaanError) as caught:
            fake.outcomes_run("voice.agent.call", _call_body(), plan=None, idempotency_key="k")
        self.assertEqual(caught.exception.code, "approval_required")

    def test_changed_input_needs_a_new_estimate(self):
        fake = FakePramaan()
        body = _call_body()
        estimate = fake.outcomes_estimate("voice.agent.call", body)
        body["input"]["to"] = "+14155550102"
        with self.assertRaises(PramaanError) as caught:
            fake.outcomes_run("voice.agent.call", body, plan=estimate["plan"], idempotency_key="k")
        self.assertEqual(caught.exception.code, "conflict")

    def test_setup_estimate_shows_a_monthly_rate(self):
        fake = FakePramaan()
        body = {
            "input": {"country": "US", "type": "local", "phoneNumber": "+14155550100", "wantsSms": False},
            "options": {},
            "basis": {"ground": "contract"},
        }
        estimate = fake.outcomes_estimate("phone.number.setup", body)
        self.assertEqual(estimate["reportedMonthlyUsd"], SETUP_MONTHLY_USD)
        report = fake.outcomes_run("phone.number.setup", body, plan=estimate["plan"], idempotency_key="buy")
        self.assertEqual(report["result"]["number"], "+14155550100")
        self.assertTrue(report["result"]["voiceEnabled"])
        self.assertEqual(fake.numbers[-1]["phoneNumber"], "+14155550100")

    def test_a_not_placed_setup_run_is_a_failure(self):
        """The live platform finished a buy without renting. The report says not_placed."""
        from ai_caller.service import Service

        fake = FakePramaan()
        fake.run_results["phone.number.setup"] = {
            "status": "done",
            "result": {
                "status": "not_placed",
                "stillNeeded": [
                    {
                        "kind": "price",
                        "detail": "Check the monthly charge; no matching purchase or status answer reported it.",
                    },
                    {"kind": "status", "detail": "No number was purchased in this run."},
                ],
            },
            "steps": [],
        }
        service = Service(fake)
        estimate = service.estimate_number_setup("US", phone_number="+14157181053")
        with self.assertRaises(PramaanError) as caught:
            service.confirm_number_setup(estimate)
        self.assertIn("not placed", str(caught.exception))
        self.assertIn("No number was purchased in this run", str(caught.exception))
        self.assertEqual(fake.numbers, [])

    def test_a_not_placed_call_run_is_saved_failed(self):
        from ai_caller.model import Agent
        from ai_caller.service import Service

        fake = FakePramaan()
        fake.run_results["voice.agent.call"] = {
            "status": "done",
            "result": {
                "status": "not_placed",
                "stillNeeded": [{"kind": "status", "detail": "The call was not placed."}],
            },
            "steps": [],
        }
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone."))
        estimate = service.estimate_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        with self.assertRaises(PramaanError) as caught:
            service.place_agent_call(
                to="+14155550101",
                from_number="+14155550100",
                assistant="reception",
                plan=estimate["plan"],
                outcome_body=estimate["body"],
            )
        self.assertIn("not placed", str(caught.exception))
        saved = service.store.list_calls()[0]
        self.assertEqual(saved["status"], "failed")
        self.assertIn("not placed", saved["failureReason"])
        self.assertTrue(saved.get("retryable", False))
        self.assertNotEqual(saved["status"], "completed")

    def test_unavailable_and_later_and_hidden(self):
        fake = FakePramaan()
        fake.outcome_state = "down"
        with self.assertRaises(OutcomeUnavailable):
            fake.outcomes_estimate("voice.agent.call", _call_body())
        fake.outcome_state = "later"
        with self.assertRaises(OutcomeLater) as later:
            fake.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k")
        self.assertEqual(str(later.exception), OPEN_RUNS_LATER)
        self.assertEqual(len([item for item in fake.calls if item["id"] == "outcome.run"]), 1)
        cancelled = fake.cancel_job("job_open")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(fake.jobs["job_open"]["status"], "cancelled")
        fake.outcome_state = "hidden"
        self.assertFalse(fake.outcomes_get("voice.agent.call")["available"])
        with self.assertRaises(OutcomeUnavailable):
            fake.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k2")


class HttpOutcomeTests(unittest.TestCase):
    def _server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_run_sends_plan_header_and_polls(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path.startswith("/v1/jobs/"):
                    raw = json.dumps(
                        {"status": "done", "result": {"status": "done", "result": {"ref": "call_job"}}}
                    ).encode()
                else:
                    raw = json.dumps({"id": "voice.agent.call", "available": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                seen["plan"] = self.headers.get("Pramaan-Plan")
                seen["key"] = self.headers.get("Idempotency-Key")
                seen["path"] = self.path
                raw = json.dumps({"job_id": "job_out", "status": "running"}).encode()
                self.send_response(202)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Handler)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            report = client.outcomes_run(
                "voice.agent.call", _call_body(), plan="plan_abc", idempotency_key="idem-1"
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(report["result"]["ref"], "call_job")
        self.assertEqual(seen["plan"], "plan_abc")
        self.assertEqual(seen["key"], "idem-1")
        self.assertEqual(seen["path"], "/v1/outcomes/voice.agent.call")

    def test_list_get_and_estimate_paths(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                seen.append(self.path)
                raw = json.dumps({"results": [{"id": "voice.agent.call", "available": True}]}).encode()
                if self.path != "/v1/outcomes":
                    raw = json.dumps({"id": "voice.agent.call", "available": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                seen.append(self.path)
                raw = json.dumps({"plan": "plan_e", "can_run": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Handler)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(client.outcomes_list()["results"][0]["id"], "voice.agent.call")
            self.assertTrue(client.outcomes_get("voice.agent.call")["available"])
            self.assertEqual(client.outcomes_estimate("voice.agent.call", _call_body())["plan"], "plan_e")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(
            seen,
            ["/v1/outcomes", "/v1/outcomes/voice.agent.call", "/v1/outcomes/voice.agent.call/estimate"],
        )

    def test_502_and_429(self):
        class Down(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                raw = b'{"error":{"code":"upstream_unavailable"}}'
                self.send_response(502)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Down)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(OutcomeUnavailable):
                client.outcomes_get("voice.agent.call")
        finally:
            server.shutdown()
            server.server_close()

        class Later(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                raw = b'{"error":{"code":"rate_limited"}}'
                self.send_response(429)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Later)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(OutcomeLater):
                client.outcomes_list()
        finally:
            server.shutdown()
            server.server_close()

    def test_hidden_outcome_is_not_run(self):
        class Hidden(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                raw = json.dumps({"id": "voice.agent.call", "available": False}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                raise AssertionError("A hidden outcome must not be posted.")

        server = self._server(Hidden)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(OutcomeUnavailable):
                client.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k")
        finally:
            server.shutdown()
            server.server_close()

    def test_starting_a_run_returns_429_once(self):
        posts = {"n": 0}

        class Later(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                raw = json.dumps({"id": "voice.agent.call", "available": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                posts["n"] += 1
                raw = b'{"error":{"code":"rate_limited"}}'
                self.send_response(429)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Later)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(OutcomeLater) as caught:
                client.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(str(caught.exception), OPEN_RUNS_LATER)
        self.assertEqual(posts["n"], 1)

    def test_open_run_ends_at_35_minutes(self):
        posts = {"n": 0}

        class Running(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path.startswith("/v1/jobs/"):
                    raw = json.dumps({"status": "running"}).encode()
                else:
                    raw = json.dumps({"id": "voice.agent.call", "available": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                posts["n"] += 1
                raw = json.dumps({"job_id": "job_slow", "status": "running"}).encode()
                self.send_response(202)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Running)
        clock = {"t": 0.0}
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            client.monotonic = lambda: clock["t"]

            def jump(_seconds: float) -> None:
                clock["t"] += RUN_LIMIT_SECONDS

            client.sleep = jump
            with self.assertRaises(PramaanError) as caught:
                client.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(caught.exception.code, "upstream_timeout")
        self.assertEqual(posts["n"], 1)

    def test_server_upstream_timeout_and_unknown_effect(self):
        class Failed(BaseHTTPRequestHandler):
            mode = "timeout"

            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path.startswith("/v1/jobs/"):
                    if Failed.mode == "timeout":
                        raw = json.dumps(
                            {"status": "failed", "error": {"code": "upstream_timeout"}, "result": {"steps": []}}
                        ).encode()
                    else:
                        raw = json.dumps(
                            {
                                "status": "failed",
                                "error": {"code": "failed"},
                                "result": {"steps": [{"effect": "unknown"}]},
                            }
                        ).encode()
                else:
                    raw = json.dumps({"id": "voice.agent.call", "available": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                raw = json.dumps({"job_id": "job_bad", "status": "running"}).encode()
                self.send_response(202)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Failed)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(PramaanError) as timed:
                client.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k1")
            Failed.mode = "unknown"
            with self.assertRaises(OutcomeCheck) as unknown:
                client.outcomes_run("voice.agent.call", _call_body(), plan="plan_x", idempotency_key="k2")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(timed.exception.code, "upstream_timeout")
        self.assertNotIsInstance(unknown.exception, OutcomeUnavailable)
        self.assertIn("Check this step", str(unknown.exception))

    def test_switched_off_capability_is_not_an_outcome_fallback(self):
        class Off(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                raw = json.dumps(
                    {"error": {"code": "upstream_unavailable", "message": "This capability is not available yet."}}
                ).encode()
                self.send_response(502)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Off)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            with self.assertRaises(PramaanError) as caught:
                client.call(
                    "voice.number.import",
                    {"number": "+14155550198", "confirm": True},
                    idempotency_key="import-off",
                )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertEqual(str(caught.exception), "This is not available yet.")
        self.assertNotIsInstance(caught.exception, OutcomeUnavailable)

    def test_cancel_posts_the_job(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                seen["path"] = self.path
                seen["key"] = self.headers.get("Idempotency-Key")
                raw = json.dumps({"status": "cancelled", "result": {"steps": [{"effect": "none"}]}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = self._server(Handler)
        try:
            client = PramaanClient("test-key", f"http://127.0.0.1:{server.server_address[1]}")
            report = client.cancel_job("job_9")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["path"], "/v1/jobs/job_9/cancel")
        self.assertTrue(seen["key"])
        self.assertEqual(report["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

"""Every schema this product can send has to fit the platform subset."""

from __future__ import annotations

import unittest
from pathlib import Path

from ai_caller.compile import tool_body
from ai_caller.evaluate import OUTCOME, schemas_for_pramaan
from ai_caller.model import Agent, Parameter, Tool
from ai_caller.pramaan import (
    SCHEMA_REFUSED,
    FakePramaan,
    PramaanError,
    SchemaError,
    bounded_schema,
    check_tool_parameters,
)
from ai_caller.service import Service

ROOT = Path(__file__).resolve().parents[1]


class SchemaSubsetTests(unittest.TestCase):
    def test_every_product_schema_fits_the_subset(self) -> None:
        sent = schemas_for_pramaan()
        self.assertEqual([name for name, _schema in sent], ["call judgement"])
        for name, schema in sent:
            self.assertEqual(bounded_schema(schema, name), schema)
            self.assertNotIn("$schema", schema)
            self.assertNotIn("format", schema)
        self.assertEqual(OUTCOME["additionalProperties"], False)
        mentions = []
        for path in (ROOT / "ai_caller").rglob("*.py"):
            text = path.read_text()
            if "jsonSchema" in text:
                mentions.append(path.name)
        self.assertEqual(sorted(mentions), ["evaluate.py", "pramaan.py"])

    def test_summary_sends_no_schema_and_tools_stay_scalar(self) -> None:
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone."))
        service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        chat = next(item for item in fake.calls if item["id"] == "llm.chat")
        self.assertNotIn("jsonSchema", chat["body"])
        self.assertNotIn("tools", chat["body"])
        tool = Tool(
            name="lookup",
            description="Look up a record.",
            parameters=[Parameter("city", "string", "City", True)],
            endpoint="we_" + "ab" * 16,
        )
        parameters = tool_body(tool)["parameters"]
        self.assertEqual(check_tool_parameters(parameters, "lookup"), parameters)
        fake.call("voice.tool.create", tool_body(tool), idempotency_key="tool-1")
        too_many = [
            {"name": f"field{i}", "type": "string", "description": "A field.", "required": False}
            for i in range(21)
        ]
        with self.assertRaises(PramaanError) as caught:
            fake.call(
                "voice.tool.create",
                {
                    "name": "wide",
                    "kind": "function",
                    "parameters": too_many,
                    "endpoint": "we_" + "cd" * 16,
                },
            )
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIn("20 scalar", str(caught.exception))
        with self.assertRaises(PramaanError):
            fake.call(
                "voice.tool.create",
                {
                    "name": "shaped",
                    "kind": "function",
                    "endpoint": "we_" + "ef" * 16,
                    "parameters": [
                        {
                            "name": "city",
                            "type": "object",
                            "description": "A city.",
                            "required": True,
                            "properties": {"name": {"type": "string"}},
                        }
                    ],
                },
            )

    def test_empty_argument_descriptions_are_filled_and_refused(self) -> None:
        """Production refuses an empty parameter description, so the compiler fills one."""
        fake = FakePramaan()
        blank = Tool(
            name="lookup",
            description="Look up a record.",
            parameters=[Parameter("city", "string", "  ", True)],
            endpoint="we_" + "ab" * 16,
        )
        body = tool_body(blank)
        self.assertEqual(body["parameters"][0]["description"], "Argument city.")
        fake.call("voice.tool.create", body, idempotency_key="tool-blank")
        empty = {
            "name": "hollow",
            "kind": "function",
            "endpoint": "we_" + "cd" * 16,
            "parameters": [{"name": "city", "type": "string", "description": "", "required": False}],
        }
        with self.assertRaises(PramaanError) as caught:
            fake.call("voice.tool.create", empty)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIn("description must not be empty", str(caught.exception))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[-1]["body"]["name"], "lookup")

    def test_fake_refuses_the_same_subset_as_the_platform(self) -> None:
        fake = FakePramaan()
        with self.assertRaises(SchemaError) as caught:
            fake.call(
                "llm.chat",
                {
                    "messages": [{"role": "user", "content": "Say yes."}],
                    "jsonSchema": {"type": "string", "format": "email"},
                },
            )
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIn(SCHEMA_REFUSED, str(caught.exception))
        self.assertIn("llm.chat jsonSchema at /type", str(caught.exception))
        self.assertEqual(fake.calls, [])
        with self.assertRaises(SchemaError) as complete:
            fake.call(
                "llm.complete",
                {
                    "messages": [{"role": "user", "content": "Say yes."}],
                    "jsonSchema": {"type": "string"},
                },
            )
        self.assertIn("llm.complete jsonSchema at /type", str(complete.exception))
        with self.assertRaises(SchemaError) as tool_schema:
            fake.call(
                "llm.chat",
                {
                    "messages": [{"role": "user", "content": "Use the tool."}],
                    "tools": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string", "pattern": "^a"}},
                            },
                        }
                    ],
                },
            )
        self.assertIn("lookup", str(tool_schema.exception))
        self.assertIn("/properties/city/pattern", str(tool_schema.exception))
        self.assertEqual(fake.calls, [])
        with self.assertRaises(SchemaError) as remote:
            fake.call(
                "llm.chat",
                {
                    "messages": [{"role": "user", "content": "Use the tool."}],
                    "tools": [
                        {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"$ref": "https://example.com/city"}},
                            },
                        }
                    ],
                },
            )
        self.assertIn("lookup", str(remote.exception))
        self.assertIn("/properties/city/$ref", str(remote.exception))
        neutral = {
            "$schema": "https://example.com/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "passed": {"type": "boolean"},
                "reason": {"type": "string", "maxLength": 500, "format": "email", "$schema": "drop"},
            },
            "required": ["passed", "reason"],
        }
        cleaned = bounded_schema(neutral, "call judgement", root_object=True)
        self.assertNotIn("$schema", cleaned)
        self.assertNotIn("format", cleaned["properties"]["reason"])
        self.assertNotIn("$schema", cleaned["properties"]["reason"])
        self.assertEqual(cleaned["required"], ["passed", "reason"])
        local = bounded_schema(
            {
                "type": "object",
                "$defs": {"city": {"type": "string"}},
                "properties": {"city": {"$ref": "#/$defs/city"}},
            },
            "llm.complete jsonSchema",
            root_object=True,
        )
        self.assertEqual(local["properties"]["city"]["$ref"], "#/$defs/city")
        accepted = fake.call(
            "llm.chat",
            {
                "messages": [{"role": "user", "content": "Judge the transcript."}],
                "jsonSchema": neutral,
            },
        )
        self.assertIn("passed", accepted["text"])
        with self.assertRaises(SchemaError) as split:
            bounded_schema({"oneOf": [{"type": "string"}, {"type": "number"}]}, "split")
        self.assertIn("split at /oneOf", str(split.exception))
        with self.assertRaises(SchemaError) as patterned:
            bounded_schema({"type": "string", "pattern": "^[a-z]+$"}, "patterned")
        self.assertIn("patterned at /pattern", str(patterned.exception))
        with self.assertRaises(SchemaError):
            fake.call(
                "document.extract",
                {"url": "https://example.com/note.pdf", "schema": {"type": "string"}},
            )
        with self.assertRaises(SchemaError) as outcome:
            fake.outcomes_estimate(
                "voice.agent.call",
                {"input": {"schema": {"type": "string", "pattern": "^a"}}},
            )
        self.assertIn("voice.agent.call schema at /pattern", str(outcome.exception))

    def test_failed_model_or_capability_call_is_not_completed(self) -> None:
        fake = FakePramaan()
        service = Service(fake)
        service.publish(Agent(name="reception", prompt="Answer the phone."))

        class SummaryFails(FakePramaan):
            def call(self, capability_id, body=None, *, idempotency_key=None):
                if capability_id == "llm.chat":
                    raise PramaanError("invalid_request", SCHEMA_REFUSED)
                return super().call(capability_id, body, idempotency_key=idempotency_key)

        failing = SummaryFails()
        failing.outcome_state = "down"
        service.client = failing
        with self.assertRaises(PramaanError):
            service.place_agent_call(
                to="+14155550101", from_number="+14155550100", assistant="reception"
            )
        saved = service.list_calls()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["status"], "failed")
        self.assertEqual(saved[0]["failureReason"], SCHEMA_REFUSED)
        self.assertTrue(saved[0]["retryable"])
        self.assertNotEqual(saved[0]["status"], "completed")
        detail = service.call_detail(saved[0]["ref"])
        self.assertEqual(detail["status"], "failed")
        self.assertTrue(all(step["status"] != "completed" for step in detail["timeline"]))
        service.client = fake
        again = service.place_agent_call(
            to="+14155550101", from_number="+14155550100", assistant="reception"
        )
        self.assertEqual(again["status"], "completed")

        class DialFails(FakePramaan):
            def call(self, capability_id, body=None, *, idempotency_key=None):
                if capability_id == "voice.call.start-agent":
                    raise PramaanError("upstream_unavailable", "The request failed.")
                return super().call(capability_id, body, idempotency_key=idempotency_key)

        service.client = DialFails()
        service.client.outcome_state = "down"
        before = len(service.list_calls())
        with self.assertRaises(PramaanError):
            service.place_agent_call(
                to="+14155550102", from_number="+14155550100", assistant="reception"
            )
        rows = service.list_calls()
        self.assertEqual(len(rows), before + 1)
        failed = next(row for row in rows if row["to"] == "+14155550102")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("The request failed.", failed["failureReason"])
        self.assertTrue(failed["retryable"])

        class BulkFails(FakePramaan):
            def call(self, capability_id, body=None, *, idempotency_key=None):
                if capability_id == "voice.call.start-agent-bulk":
                    raise PramaanError("upstream_unavailable", "The request failed.")
                return super().call(capability_id, body, idempotency_key=idempotency_key)

        service.client = BulkFails()
        with self.assertRaises(PramaanError):
            service.start_agent_campaign(
                "Notice", "+14155550100", "reception", [{"id": "a", "to": "+14155550103"}]
            )
        campaign = service.list_campaigns()[0]
        self.assertEqual(campaign["state"], "failed")
        self.assertNotEqual(campaign["state"], "completed")
        self.assertTrue(campaign["retryable"])
        self.assertIn("The request failed.", campaign["failureReason"])
        service.client = fake
        retried = service.start_agent_campaign(
            "Notice", "+14155550100", "reception", [{"id": "b", "to": "+14155550104"}]
        )
        self.assertEqual(retried["state"], "completed")


if __name__ == "__main__":
    unittest.main()

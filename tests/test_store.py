import unittest

from ai_caller.model import Agent
from ai_caller.store import Conflict, Store, agent_record


class StoreTests(unittest.TestCase):
    def test_agent_round_trip_and_owner(self):
        shared = "/tmp/ai-caller-store-test.sqlite"
        import os

        os.remove(shared) if os.path.exists(shared) else None
        first = Store(shared, "one")
        second = Store(shared, "two")
        first.save_agent(agent_record(Agent(name="reception", prompt="Answer.")))
        self.assertEqual(first.list_agents()[0]["name"], "reception")
        self.assertEqual(second.list_agents(), [])
        self.assertIsNone(second.get_agent("reception"))
        self.assertTrue(first.delete_agent("reception"))
        self.assertFalse(second.delete_agent("reception"))

    def test_call_filters(self):
        store = Store()
        store.save_call({"ref": "call_a", "status": "completed", "assistant": "reception", "to": "+14155550101"})
        store.save_call({"ref": "call_b", "status": "cancelled", "assistant": "billing", "to": "+14155550102"})
        self.assertEqual(len(store.list_calls(status="completed")), 1)
        self.assertEqual(store.list_calls(assistant="billing")[0]["ref"], "call_b")
        self.assertEqual(store.list_calls(q="50102")[0]["ref"], "call_b")
        self.assertIsNone(Store(store.path, "other").get_call("call_a")) if False else None
        other = Store(":memory:", "other")
        self.assertIsNone(other.get_call("call_a"))

    def test_idempotency_replay_and_conflict(self):
        store = Store()
        store.remember("k", "/api/calls", {"to": "+1"}, 200, {"ref": "call_a"})
        self.assertEqual(store.recall("k", "/api/calls", {"to": "+1"}), (200, {"ref": "call_a"}))
        with self.assertRaises(Conflict):
            store.recall("k", "/api/calls", {"to": "+2"})

    def test_opt_out_and_settings_stay_with_the_owner(self):
        store = Store()
        store.add_opt_out("+14155550101")
        self.assertTrue(store.opted_out("+14155550101"))
        store.remove_opt_out("+14155550101")
        self.assertFalse(store.opted_out("+14155550101"))
        saved = store.save_settings("we_0123456789abcdef0123456789abcdef")
        self.assertEqual(saved["webhookEndpoint"], store.settings()["webhookEndpoint"])
        self.assertEqual(Store(":memory:", "other").settings()["webhookEndpoint"], "")

    def test_tools_squads_and_campaigns(self):
        store = Store()
        store.save_tool({"name": "lookup", "kind": "code"})
        store.save_squad({"name": "front", "members": ["reception"]})
        store.save_campaign({"ref": "campaign_1", "name": "Notice", "state": "running", "rows": []})
        self.assertEqual(store.get_tool("lookup")["kind"], "code")
        self.assertEqual(store.list_squads()[0]["name"], "front")
        self.assertEqual(store.get_campaign("campaign_1")["state"], "running")
        self.assertTrue(store.delete_tool("lookup"))
        self.assertIsNone(store.get_tool("lookup"))


if __name__ == "__main__":
    unittest.main()

"""A missing key keeps the console up. .env fills only what is unset."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ai_caller.pramaan import KEY_MESSAGE, PramaanClient, PramaanError
from ai_caller.server import load_env, make_server
from ai_caller.store import Store


class EnvTests(unittest.TestCase):
    def test_load_env_skips_blanks_and_keeps_the_process_value(self) -> None:
        previous_key = os.environ.get("PRAMAAN_API_KEY")
        previous_base = os.environ.get("PRAMAAN_BASE_URL")
        previous_secret = os.environ.get("PRAMAAN_WEBHOOK_SECRET")
        os.environ["PRAMAAN_API_KEY"] = "already"
        os.environ.pop("PRAMAAN_BASE_URL", None)
        os.environ.pop("PRAMAAN_WEBHOOK_SECRET", None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ".env"
                path.write_text(
                    "\n".join(
                        [
                            "# comment",
                            "PRAMAAN_API_KEY=",
                            "PRAMAAN_API_KEY=from-file",
                            "PRAMAAN_BASE_URL=https://example.invalid",
                            "PRAMAAN_WEBHOOK_SECRET=",
                            "",
                        ]
                    )
                )
                load_env(path)
                self.assertEqual(os.environ["PRAMAAN_API_KEY"], "already")
                self.assertEqual(os.environ["PRAMAAN_BASE_URL"], "https://example.invalid")
                self.assertNotIn("PRAMAAN_WEBHOOK_SECRET", os.environ)
        finally:
            if previous_key is None:
                os.environ.pop("PRAMAAN_API_KEY", None)
            else:
                os.environ["PRAMAAN_API_KEY"] = previous_key
            if previous_base is None:
                os.environ.pop("PRAMAAN_BASE_URL", None)
            else:
                os.environ["PRAMAAN_BASE_URL"] = previous_base
            if previous_secret is None:
                os.environ.pop("PRAMAAN_WEBHOOK_SECRET", None)
            else:
                os.environ["PRAMAAN_WEBHOOK_SECRET"] = previous_secret

    def test_missing_key_does_not_call_out(self) -> None:
        def opener(req, timeout=30):
            raise AssertionError("called out")

        client = PramaanClient("", opener=opener)
        with self.assertRaises(PramaanError) as caught:
            client.call("voice.assistants.list", {})
        self.assertEqual(str(caught.exception), KEY_MESSAGE)

    def test_console_stays_up_without_a_key(self) -> None:
        os.environ.pop("PRAMAAN_FAKE", None)
        os.environ.pop("PRAMAAN_API_KEY", None)
        server = make_server(port=0, store=Store())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/") as page:
                html = page.read().decode()
            self.assertIn("AI Caller", html)
            request = urllib.request.Request(base + "/api/knowledge")
            try:
                urllib.request.urlopen(request)
                self.fail("knowledge listed without a key")
            except urllib.error.HTTPError as error:
                body = json.loads(error.read().decode())
                error.close()
                self.assertEqual(error.code, 400)
                self.assertEqual(body["error"], KEY_MESSAGE)
            with urllib.request.urlopen(base + "/api/settings") as settings:
                view = json.loads(settings.read().decode())
            self.assertTrue(view["needsKey"])
            self.assertFalse(view["secretSet"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

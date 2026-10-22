"""Managed database: save the address once, reuse it, or keep the local file."""

from __future__ import annotations

import io
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ai_caller.database import (
    LOCAL_LINE,
    MISSING_LINE,
    open_database,
    saved_action_key,
)
from ai_caller.pramaan import FakePramaan, PramaanError
from ai_caller.server import _ready_store
from ai_caller.store import _sql


SECRET = "postgresql://app:saved-secret@127.0.0.1/app"
DIRECT = "postgresql://app:saved-secret@127.0.0.1/app?direct=1"


class _SpentKeys:
    """The platform: a shown result is not shown again. A repeat answers conflict."""

    def __init__(self, inner, spent: str) -> None:
        self.inner = inner
        self.spent = spent

    def call(self, capability_id: str, body=None, *, idempotency_key=None):
        if idempotency_key == self.spent:
            raise PramaanError("conflict", "The request failed.")
        return self.inner.call(capability_id, body, idempotency_key=idempotency_key)


class _AlwaysConflict:
    def __init__(self, inner, capability_id: str) -> None:
        self.inner = inner
        self.capability_id = capability_id

    def call(self, capability_id: str, body=None, *, idempotency_key=None):
        if capability_id == self.capability_id:
            raise PramaanError("conflict", "The request failed.")
        return self.inner.call(capability_id, body, idempotency_key=idempotency_key)


class DatabaseTests(unittest.TestCase):
    def test_provision_saves_the_address_once_and_does_not_print_it(self) -> None:
        fake = FakePramaan()
        fake.database_mode = "once"
        fake.database_url = SECRET
        fake.database_direct = DIRECT
        with self._folder() as raw:
            folder = Path(raw)
            printed = io.StringIO()
            with redirect_stdout(printed):
                chosen, notice = open_database(fake, folder=folder, local="local.db")
            self.assertEqual(chosen, SECRET)
            self.assertEqual(notice, "")
            self.assertNotIn(SECRET, printed.getvalue())
            self.assertNotIn("saved-secret", notice)
            secret = folder / "database"
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            self.assertEqual(secret.read_text().splitlines(), [SECRET, DIRECT])
            self.assertEqual(
                [item["id"] for item in fake.calls if item["id"] == "db.postgres.provision"],
                ["db.postgres.provision"],
            )
            self.assertEqual(fake.calls[-1]["body"], {})

    def test_reuse_keeps_the_saved_address(self) -> None:
        fake = FakePramaan()
        fake.database_mode = "once"
        fake.database_url = SECRET
        fake.database_direct = DIRECT
        with self._folder() as raw:
            folder = Path(raw)
            first, _notice = open_database(fake, folder=folder, local="local.db")
            saved = (folder / "database").read_bytes()
            second, notice = open_database(fake, folder=folder, local="local.db")
            self.assertEqual(first, SECRET)
            self.assertEqual(second, SECRET)
            self.assertEqual(notice, "")
            self.assertEqual((folder / "database").read_bytes(), saved)
            provisions = [item for item in fake.calls if item["id"] == "db.postgres.provision"]
            self.assertEqual(len(provisions), 1)
            self.assertEqual(provisions[0]["body"], {})
            self.assertTrue(provisions[0]["idempotency_key"].startswith("managed-database-"))
            saved_key = (folder / "idempotency").read_text()
            self.assertIn(provisions[0]["idempotency_key"], saved_key)
            self.assertEqual(saved_key.count("managed-database-"), 1)
            self.assertNotIn(provisions[0]["idempotency_key"], notice)
            self.assertNotIn("connectionString", str(provisions[0]))

    def test_unavailable_uses_the_local_database(self) -> None:
        fake = FakePramaan()
        with self._folder() as raw:
            folder = Path(raw)
            printed = io.StringIO()
            with redirect_stdout(printed):
                chosen, notice = open_database(fake, folder=folder, local="local.db")
            self.assertEqual(chosen, "local.db")
            self.assertEqual(notice, LOCAL_LINE)
            self.assertNotIn("saved-secret", printed.getvalue())
            self.assertFalse((folder / "database").exists())
            self.assertEqual(len(fake.calls), 1)

    def test_missing_address_uses_the_local_database(self) -> None:
        fake = FakePramaan()
        fake.database_mode = "once"
        fake.database_url = ""
        with self._folder() as raw:
            folder = Path(raw)
            chosen, notice = open_database(fake, folder=folder, local="local.db")
            self.assertEqual(chosen, "local.db")
            self.assertEqual(notice, MISSING_LINE)
            self.assertFalse((folder / "database").exists())

    def test_a_spent_saved_key_is_rotated_and_the_call_runs_again(self) -> None:
        """The platform shows a spent key's result once, then answers conflict."""
        fake = FakePramaan()
        fake.database_mode = "once"
        fake.database_url = SECRET
        with self._folder() as raw:
            folder = Path(raw)
            spent = saved_action_key("db.postgres.provision", folder=folder)
            platform = _SpentKeys(fake, spent=spent)
            chosen, notice = open_database(platform, folder=folder, local="local.db")
            self.assertEqual(chosen, SECRET)
            self.assertEqual(notice, "")
            saved = (folder / "idempotency").read_text()
            self.assertIn("managed-database-", saved)
            self.assertNotIn(spent, saved)
            fresh = [item for item in fake.calls if item["id"] == "db.postgres.provision"]
            self.assertEqual(len(fresh), 1)
            self.assertNotEqual(fresh[0]["idempotency_key"], spent)

    def test_a_conflict_that_survives_rotation_keeps_the_console_up(self) -> None:
        fake = FakePramaan()
        with self._folder() as raw:
            folder = Path(raw)
            platform = _AlwaysConflict(fake, "db.postgres.provision")
            chosen, notice = open_database(platform, folder=folder, local="local.db")
            self.assertEqual(chosen, "local.db")
            self.assertEqual(notice, LOCAL_LINE)

    def test_a_closed_managed_database_does_not_print_the_address(self) -> None:
        fake = FakePramaan()
        fake.database_mode = "once"
        fake.database_url = "postgresql://app:saved-secret@127.0.0.1:1/app"
        fake.database_direct = "postgresql://app:saved-secret@127.0.0.1:1/app"
        previous = os.environ.get("PRAMAAN_API_KEY")
        previous_db = os.environ.get("AI_CALLER_DB")
        os.environ["PRAMAAN_API_KEY"] = "test-key"
        try:
            with self._folder() as raw:
                folder = Path(raw)
                os.environ["AI_CALLER_DB"] = str(folder / "local.db")
                printed = io.StringIO()
                errors = io.StringIO()
                with redirect_stdout(printed), redirect_stderr(errors):
                    store, notice = _ready_store(fake, folder=folder)
                store.close()
                self.assertEqual(notice, LOCAL_LINE)
                self.assertNotIn("saved-secret", printed.getvalue())
                self.assertNotIn("saved-secret", errors.getvalue())
                self.assertNotIn("saved-secret", notice)
                self.assertTrue((folder / "local.db").exists())
        finally:
            if previous is None:
                os.environ.pop("PRAMAAN_API_KEY", None)
            else:
                os.environ["PRAMAAN_API_KEY"] = previous
            if previous_db is None:
                os.environ.pop("AI_CALLER_DB", None)
            else:
                os.environ["AI_CALLER_DB"] = previous_db

    def test_managed_statements_keep_the_same_shape(self) -> None:
        ignore = "INSERT OR IGNORE INTO opt_outs (owner, address, channel) VALUES (?, ?, ?)"
        rewritten = _sql(ignore, True)
        self.assertIn("ON CONFLICT DO NOTHING", rewritten)
        self.assertNotIn("?", rewritten)
        replaced = _sql("INSERT OR REPLACE INTO idempotency (owner, key) VALUES (?, ?)", True)
        self.assertIn("ON CONFLICT (owner, key)", replaced)
        self.assertNotIn("?", replaced)

    def test_a_missing_key_is_printed(self) -> None:
        from ai_caller.pramaan import IDEMPOTENCY_REQUIRED, PramaanError

        class Refuse:
            def call(self, *_args, **_kwargs):
                raise PramaanError("invalid_request", IDEMPOTENCY_REQUIRED)

        with self._folder() as raw:
            folder = Path(raw)
            printed = io.StringIO()
            with redirect_stdout(printed):
                chosen, notice = open_database(Refuse(), folder=folder, local="local.db")
            self.assertEqual(chosen, "local.db")
            self.assertEqual(notice, IDEMPOTENCY_REQUIRED)
            key = (folder / "idempotency").read_text().split("\t", 1)[1].strip()
            self.assertNotIn(key, notice)
            self.assertNotIn(key, printed.getvalue())
            self.assertFalse((folder / "database").exists())

    def _folder(self):
        import tempfile

        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()

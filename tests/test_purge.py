"""Удаление старых заявок: срок хранения и файлы артефактов."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import sqlite3
import unittest
import uuid
from argparse import Namespace
from datetime import datetime, timedelta, timezone

from kpbot.cli import cmd_purge
from kpbot.config import settings
from kpbot.store import Store

TMP = support.subdir("purge")


class PurgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP / f"data-{uuid.uuid4().hex[:8]}"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.original = settings.data_dir
        settings.data_dir = self.data_dir
        self.store = Store(settings.db_path)
        self.old_id = "b" * 32
        self.new_id = "c" * 32
        for rfq_id, message_id in ((self.old_id, "<old@example.ru>"), (self.new_id, "<new@example.ru>")):
            self.store.save_rfq(
                rfq_id=rfq_id,
                message_id=message_id,
                sender="a@b.ru",
                subject="Заявка",
                received_at="2026-01-01T00:00:00+00:00",
                status="draft_ready",
                raw_text="уголок 50х50 — 1 шт",
                lines=[],
            )
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        with sqlite3.connect(str(settings.db_path)) as conn:
            conn.execute("UPDATE rfqs SET created_at = ? WHERE id = ?", (old_date, self.old_id))
        self.artifact = self.data_dir / "old.html"
        self.artifact.write_text("kp", encoding="utf-8")
        self.store.add_artifact(self.old_id, "kp_html", self.artifact, "0" * 64)

    def tearDown(self) -> None:
        settings.data_dir = self.original
        shutil.rmtree(TMP, ignore_errors=True)

    def test_dry_run_changes_nothing(self) -> None:
        result = self.store.purge_older_than(90, dry_run=True)
        self.assertEqual(result["rfqs"], 1)
        self.assertIsNotNone(self.store.get_rfq(self.old_id))
        self.assertTrue(self.artifact.exists())

    def test_delete_removes_rfq_artifact_and_keeps_recent(self) -> None:
        code = cmd_purge(Namespace(days=90, tenant="", all=False, yes=True))
        self.assertEqual(code, 0)
        self.assertIsNone(self.store.get_rfq(self.old_id))
        self.assertIsNotNone(self.store.get_rfq(self.new_id))
        self.assertFalse(self.artifact.exists())


if __name__ == "__main__":
    unittest.main()

"""Бэкап с проверкой целостности и восстановление базы."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest
from argparse import Namespace
from pathlib import Path

from kpbot.cli import _backup_one, cmd_restore
from kpbot.config import settings
from kpbot.pricelist import default_price_items
from kpbot.store import Store

TMP = support.subdir("backup-restore")


class BackupRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP / "data"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.original_data_dir = settings.data_dir
        settings.data_dir = self.data_dir
        self.store = Store(settings.db_path)
        self.store.replace_price_items(default_price_items())
        self.assertEqual(self.store.price_count(), 55)

    def tearDown(self) -> None:
        settings.data_dir = self.original_data_dir
        shutil.rmtree(TMP, ignore_errors=True)

    def _latest_backup(self) -> Path:
        backups = sorted((self.data_dir / "backups").glob("kpbot-*.sqlite3"))
        self.assertTrue(backups, "бэкап не создан")
        return backups[-1]

    def test_backup_reports_integrity_and_restore_returns_data(self) -> None:
        report = _backup_one(settings.db_path, keep=3)
        self.assertIn("целостность ok", report)
        backup = self._latest_backup()

        self.store.replace_price_items(default_price_items()[:1])
        self.assertEqual(self.store.price_count(), 1)

        code = cmd_restore(Namespace(file=str(backup), tenant="", yes=True))
        self.assertEqual(code, 0)
        self.assertEqual(Store(settings.db_path).price_count(), 55)
        # страховочная копия «до восстановления» тоже создана
        self.assertTrue(list(self.data_dir.glob("kpbot.before-restore-*.sqlite3")))

    def test_restore_refuses_corrupted_copy(self) -> None:
        bad = self.data_dir / "bad.sqlite3"
        bad.write_bytes(b"not a sqlite database at all")
        code = cmd_restore(Namespace(file=str(bad), tenant="", yes=True))
        self.assertEqual(code, 2)

    def test_restore_requires_confirmation(self) -> None:
        backup = self._latest_backup() if (self.data_dir / "backups").exists() else None
        if backup is None:
            _backup_one(settings.db_path, keep=3)
            backup = self._latest_backup()
        code = cmd_restore(Namespace(file=str(backup), tenant="", yes=False))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

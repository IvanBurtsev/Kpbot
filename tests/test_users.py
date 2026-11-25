"""Пользователи, роли и разделение заявок."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest

from kpbot.config import BASE_DIR, Settings
from kpbot.pipeline import Pipeline
from kpbot.store import Store
from kpbot.users import ROLE_ADMIN, ROLE_MANAGER, hash_password, verify_password

TMP = support.subdir("users")


class PasswordTest(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self) -> None:
        salt1, hash1 = hash_password("секрет123")
        salt2, hash2 = hash_password("секрет123")
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(hash1, hash2)
        self.assertTrue(verify_password("секрет123", salt1, hash1))
        self.assertFalse(verify_password("другой", salt1, hash1))


class RolesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True)
        self.settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            llm_enabled=False,
        )
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_default_admin_is_created_once(self) -> None:
        admin = self.store.ensure_default_admin("test-pass")
        self.assertIsNotNone(admin)
        self.assertEqual(admin.role, ROLE_ADMIN)
        self.assertIsNone(self.store.ensure_default_admin("test-pass"))
        self.assertEqual(self.store.user_count(), 1)

    def test_login_checks_password(self) -> None:
        self.store.create_user(email="M@Example.ru", password="managerpass", role=ROLE_MANAGER)
        self.assertIsNotNone(self.store.authenticate("m@example.ru", "managerpass"))
        self.assertIsNone(self.store.authenticate("m@example.ru", "неверный"))

    def test_short_password_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_user(email="a@b.ru", password="123")

    def test_manager_sees_only_own_and_free(self) -> None:
        manager = self.store.create_user(email="m@b.ru", password="managerpass", role=ROLE_MANAGER)
        first = self.pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Первая")
        second = self.pipeline.process_text("кабель 3х2.5 — 5 м", sender="c@d.ru", subject="Вторая")

        # обе свободны — менеджер видит обе
        visible = self.store.list_rfqs(100, assignee=manager.id)
        self.assertEqual(len(visible), 2)

        self.store.set_assignee(first.rfq_id, manager.id)
        self.store.set_assignee(second.rfq_id, "другой-менеджер")
        visible = self.store.list_rfqs(100, assignee=manager.id)
        self.assertEqual([row["id"] for row in visible], [first.rfq_id])

        # админ видит всё
        self.assertEqual(len(self.store.list_rfqs(100)), 2)


if __name__ == "__main__":
    unittest.main()

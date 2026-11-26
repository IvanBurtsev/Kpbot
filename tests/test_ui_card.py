"""Карточка заявки: письмо рядом с разбором, подсветка, история отправок."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("ui_card")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, pipeline, settings, store  # noqa: E402


class RfqCardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def test_card_shows_letter_next_to_parsed_lines(self) -> None:
        result = pipeline.process_text(
            "Нужно 200 шт уголок 50х50 и подрозетник скрытой установки — 10 шт",
            sender="client@example.ru",
            subject="Заявка на материалы",
        )
        page = self.client.get(f"/ui/rfq/{result.rfq_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Письмо клиента", page.text)
        self.assertIn("Что понял сервис", page.text)
        self.assertIn("уголок 50х50", page.text)
        self.assertIn("client@example.ru", page.text)

    def test_unmatched_line_is_highlighted(self) -> None:
        result = pipeline.process_text(
            "1. монтаж кабель-канала — 100 м\n", sender="a@b.ru", subject="Заявка"
        )
        page = self.client.get(f"/ui/rfq/{result.rfq_id}")
        self.assertIn("needs-attention", page.text)
        self.assertIn("проверить:", page.text)
        self.assertIn("не найдено:", page.text)  # расшифровка, а не просто число

    def test_send_history_is_shown(self) -> None:
        result = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        store.log(result.rfq_id, "sent", "unittest", "кому client@example.ru, вложений 2")
        page = self.client.get(f"/ui/rfq/{result.rfq_id}")
        self.assertIn("История", page.text)
        self.assertIn("отправлено", page.text)
        self.assertIn("client@example.ru", page.text)

    def test_failed_send_is_visible(self) -> None:
        result = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        store.log(result.rfq_id, "send_failed", "unittest", "кому a@b.ru: связи нет")
        page = self.client.get(f"/ui/rfq/{result.rfq_id}")
        self.assertIn("не ушло", page.text)


if __name__ == "__main__":
    unittest.main()

"""Сквозной сценарий глазами пользователя: письмо → карточка → одобрение → экспорт.

Это не браузерный e2e (Playwright в этой среде не ставится), но весь путь
проходит через HTTP-слой приложения, с сессией, CSRF и теми же формами,
которые видит менеджер.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import unittest

from fastapi.testclient import TestClient

from kpbot.security import SESSION_COOKIE, csrf_for
from kpbot.web import app, settings, store


class E2EFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        response = self.client.post("/ui/login", data={"password": settings.ui_password})
        self.assertEqual(response.status_code, 303)
        session = self.client.cookies.get(SESSION_COOKIE)
        self.csrf = csrf_for(settings.session_secret, session)

    def test_letter_becomes_approved_quote_and_export(self) -> None:
        text = "уголок 50х50 — 200 шт" + chr(10) + "кабель ВВГ 3х2.5 — 150 м"
        created = self.client.post(
            "/api/v1/rfqs",
            json={
                "sender": "zakaz@stroymontazh.example",
                "subject": "Заявка на материалы",
                "text": text,
            },
            headers={"X-API-Key": settings.api_key},
        )
        self.assertIn(created.status_code, (200, 201))
        body = created.json()
        rfq_id = body["rfq_id"]
        self.assertGreaterEqual(body["matched"], 1)
        self.assertEqual(store.get_rfq(rfq_id)["status"], "draft_ready")

        card = self.client.get(f"/ui/rfq/{rfq_id}")
        self.assertEqual(card.status_code, 200)
        self.assertIn("Уголок", card.text)
        self.assertIn("Кабель", card.text)

        approved = self.client.post(
            f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": self.csrf}
        )
        self.assertEqual(approved.status_code, 303)
        # одобрение сохраняет черновик письма в outbox и переводит заявку в approved;
        # sent ставит только фактическая отправка по SMTP
        self.assertEqual(store.get_rfq(rfq_id)["status"], "approved")

        export = self.client.get(f"/ui/rfq/{rfq_id}/export.xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export.headers["content-type"])
        self.assertGreater(len(export.content), 1000)

        stats = self.client.get("/ui/stats?days=30")
        self.assertEqual(stats.status_code, 200)

    def test_unauthenticated_user_is_sent_to_login(self) -> None:
        anonymous = TestClient(app, follow_redirects=False)
        page = anonymous.get("/ui")
        self.assertEqual(page.status_code, 303)
        self.assertIn("/ui/login", page.headers["location"])

    def test_csrf_is_required_for_approve(self) -> None:
        created = self.client.post(
            "/api/v1/rfqs",
            json={"sender": "a@b.ru", "subject": "Заявка", "text": "уголок 50х50 — 5 шт"},
            headers={"X-API-Key": settings.api_key},
        )
        rfq_id = created.json()["rfq_id"]
        response = self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": "wrong"})
        self.assertIn(response.status_code, (400, 403))
        self.assertNotEqual(store.get_rfq(rfq_id)["status"], "sent")


if __name__ == "__main__":
    unittest.main()

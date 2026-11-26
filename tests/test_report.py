"""Отчёт пилота: цифры для клиента и CLI-выгрузка."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest
from argparse import Namespace

from fastapi.testclient import TestClient

from kpbot.cli import cmd_pilot_report
from kpbot.config import settings
from kpbot.report import build_pilot_report
from kpbot.web import app, pipeline, store

TMP = support.subdir("pilot-report")


class PilotReportTest(unittest.TestCase):
    def setUp(self) -> None:
        pipeline.process_text("уголок 50х50 — 2 шт", sender="client@example.ru", subject="Заявка")

    def test_web_report_has_metrics(self) -> None:
        client = TestClient(app, follow_redirects=False)
        client.post("/ui/login", data={"password": settings.ui_password})
        response = client.get("/ui/report?days=30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Отчёт пилота", response.text)
        self.assertIn("строк разобрано без человека", response.text)
        self.assertIn("Стоимость разбора", response.text)

    def test_report_builder_without_data(self) -> None:
        data_dir = TMP / "empty"
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True)
        from kpbot.store import Store

        html = build_pilot_report(Store(data_dir / "kpbot.sqlite3"), days=7)
        self.assertIn("Отчёт пилота", html)
        self.assertIn("Нет данных за период", html)

    def test_cli_writes_html(self) -> None:
        out = TMP / "report.html"
        code = cmd_pilot_report(Namespace(days=30, tenant="", out=str(out)))
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        self.assertIn("Отчёт пилота", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

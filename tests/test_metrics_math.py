"""Метрики: отчёт, время, расход LLM и «что не нашли» — на известных данных."""

import support

import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from kpbot.domain import LineStatus, PriceItem, RfqLine, price_quote
from kpbot.report import build_pilot_report
from kpbot.store import Store

TMP = support.subdir("metrics_math")


def _store(name: str) -> Store:
    return Store(TMP / f"{name}-{uuid.uuid4().hex[:8]}.sqlite3")


def _lines(qty: str = "10") -> list[RfqLine]:
    return [
        RfqLine(idx=1, raw_text="уголок", qty=Decimal(qty), unit="шт",
                matched_sku="A", status=LineStatus.OK),
        RfqLine(idx=2, raw_text="непонятное", qty=Decimal("1"), unit="шт",
                matched_sku=None, status=LineStatus.NO_MATCH),
    ]


class ReportMetricTest(unittest.TestCase):
    def test_report_counts_rfqs_and_latest_quote(self) -> None:
        store = _store("report")
        lines = _lines()
        for index, received in enumerate(("2026-11-27T10:00:00+00:00", "2026-11-27T11:00:00+00:00")):
            rfq_id = f"{index}" * 32
            store.save_rfq(
                rfq_id=rfq_id, message_id=f"m{index}", sender="a@b.ru", subject="s",
                received_at=received, status="draft_ready", raw_text="x", lines=lines,
            )
            # две версии КП у первой заявки: в отчёт должна попасть последняя
            for version, total in enumerate(("414.00", "420.90") if index == 0 else ("420.90",)):
                quote = price_quote(
                    lines,
                    {"A": PriceItem("A", "Уголок", "шт", Decimal(total) / Decimal("10") / Decimal("1.22"))},
                    vat_rate=Decimal("0.22"),
                )
                store.save_quote(f"q{index}-{version}", rfq_id, quote, duration_ms=1000 + version * 1000)
        report = store.report(3650)
        self.assertEqual(report["rfqs"], 2)
        self.assertEqual(report["quotes"], 2, "одна последняя версия КП на заявку")
        self.assertEqual(report["lines"], 4)
        self.assertEqual(report["auto_lines"], 2)
        self.assertEqual(report["auto_share"], 50.0)
        # по одной версии КП на заявку, у обеих первый расчёт 1 с
        self.assertEqual(report["median_seconds"], 1.0)

    def test_unmatched_report_aggregates(self) -> None:
        store = _store("unmatched")
        for index in range(2):
            rfq_id = f"c{index}" * 16
            store.save_rfq(
                rfq_id=rfq_id[:32], message_id=f"u{index}", sender="a@b.ru", subject="s",
                received_at="", status="human_review", raw_text="x", lines=_lines(),
            )
        rows = store.unmatched_report(3650)
        self.assertEqual(rows[0]["text"], "непонятное")
        self.assertEqual(rows[0]["count"], 2)

    def test_llm_cost_totals(self) -> None:
        store = _store("llm")
        store.save_rfq(rfq_id="d" * 32, message_id="l1", sender="a@b.ru", subject="s",
                       received_at="", status="draft_ready", raw_text="x", lines=[])
        store.add_llm_usage("d" * 32, model="m", prompt_tokens=1000, completion_tokens=200,
                            cost_rub="0.05")
        store.add_llm_usage("d" * 32, model="m", prompt_tokens=500, completion_tokens=100,
                            cost_rub="0.03")
        totals = store.llm_totals()
        self.assertEqual(totals["calls"], 2)
        self.assertEqual(totals["prompt_tokens"], 1500)
        self.assertEqual(totals["completion_tokens"], 300)
        self.assertAlmostEqual(totals["cost_rub"], 0.08, places=4)
        per_rfq = store.llm_usage_for("d" * 32)
        self.assertEqual(per_rfq["calls"], 2)
        self.assertAlmostEqual(per_rfq["cost_rub"], 0.08, places=4)

    def test_pilot_report_uses_fixed_metrics(self) -> None:
        store = _store("pilot")
        store.save_rfq(rfq_id="e" * 32, message_id="p1", sender="a@b.ru", subject="s",
                       received_at="", status="draft_ready", raw_text="x", lines=_lines())
        quote = price_quote(
            [_lines()[0]], {"A": PriceItem("A", "Уголок", "шт", Decimal("34.50"))},
            vat_rate=Decimal("0.22"),
        )
        store.save_quote("p1", "e" * 32, quote, duration_ms=5000)
        html = build_pilot_report(store, days=3650, company="Тест")
        self.assertIn("Заявок", html)
        self.assertIn("Медиана", html)


if __name__ == "__main__":
    unittest.main()

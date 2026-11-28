"""Инварианты денег, документов и метрик.

Не «примеры на глаз», а проверки, которые должны выполняться при любых
входных данных: строки складываются в итог, НДС построчно равен итоговому,
счёт в 1С повторяет снимок КП, Excel не показывает сумму с НДС под видом
суммы без НДС, метрика времени считает заявки, а не версии КП.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import json
import random
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import httpx

from kpbot.domain import LineStatus, PriceItem, QuoteLine, RfqLine, money, price_quote
from kpbot.export import build_rfq_xlsx
from kpbot.one_c import OneCClient
from kpbot.pipeline import _reply_totals
from kpbot.store import Store

TMP = support.subdir("math")
RATES = (Decimal("0.22"), Decimal("0.10"), Decimal("0.07"), Decimal("0.05"), Decimal("0"))
CENT = Decimal("0.01")


def _lines(count: int, rng: random.Random) -> tuple[list[RfqLine], dict[str, PriceItem]]:
    lines: list[RfqLine] = []
    items: dict[str, PriceItem] = {}
    for index in range(count):
        sku = f"S{index}"
        qty = Decimal(rng.randint(1, 2000)) / 100
        price = Decimal(rng.randint(1, 50000)) / 100
        lines.append(
            RfqLine(idx=index + 1, raw_text=sku, qty=qty, unit="шт",
                    matched_sku=sku, status=LineStatus.OK)
        )
        items[sku] = PriceItem(sku, f"Товар {index}", "шт", price)
    return lines, items


class MoneyInvariantTest(unittest.TestCase):
    def test_random_quotes_add_up(self) -> None:
        rng = random.Random(20261128)
        checked = 0
        for _ in range(1500):
            rate = rng.choice(RATES)
            inclusive = rng.random() < 0.4
            lines, items = _lines(rng.randint(1, 8), rng)
            delivery = Decimal(rng.randint(0, 10000)) / 100
            quote = price_quote(
                lines, items, vat_rate=rate, delivery=delivery,
                prices_include_vat=inclusive,
            )
            checked += 1
            # 1. Строка: нет/НДС/с НДС согласованы.
            for line in quote.lines:
                self.assertEqual(
                    line.net + line.vat,
                    (line.net + line.vat).quantize(CENT),
                    "нет + НДС должны быть в копейках",
                )
                if inclusive and rate > 0:
                    self.assertEqual(line.amount, line.net + line.vat, "с НДС = без НДС + НДС")
                else:
                    self.assertEqual(line.net, line.amount)
                    expected_vat = money(line.net * rate) if rate else Decimal("0.00")
                    self.assertEqual(line.vat, expected_vat, "НДС строки = сумма без НДС × ставка")
            # 2. Итоги собраны из строк.
            self.assertEqual(
                quote.subtotal, sum((line.net for line in quote.lines), Decimal("0")),
                "subtotal = сумма строк без НДС",
            )
            goods_vat = sum((line.vat for line in quote.lines), Decimal("0"))
            self.assertEqual(
                quote.vat, (goods_vat + quote.delivery_vat).quantize(CENT),
                "НДС итого = построчный НДС + НДС доставки",
            )
            # 3. Итог = без НДС + НДС + доставка (доставка в своей базе).
            delivery_base = quote.delivery - quote.delivery_vat if inclusive else quote.delivery
            self.assertEqual(
                quote.total, quote.subtotal + quote.vat + delivery_base,
                "Итого = сумма без НДС + НДС + доставка",
            )
            # 4. Всё в копейках.
            for value in (quote.subtotal, quote.vat, quote.delivery, quote.total):
                self.assertEqual(value, value.quantize(CENT), f"не копейки: {value}")
        self.assertEqual(checked, 1500)

    def test_inclusive_line_vat_is_extracted_not_added(self) -> None:
        line = RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                       matched_sku="A", status=LineStatus.OK)
        item = PriceItem("A", "Товар", "шт", Decimal("122.00"))
        quote = price_quote([line], {"A": item}, vat_rate=Decimal("0.22"),
                            prices_include_vat=True)
        self.assertEqual(quote.subtotal, Decimal("100.00"))
        self.assertEqual(quote.vat, Decimal("22.00"))
        self.assertEqual(quote.total, Decimal("122.00"))

    def test_zero_rate_has_no_vat(self) -> None:
        line = RfqLine(idx=1, raw_text="x", qty=Decimal("3"), unit="шт",
                       matched_sku="A", status=LineStatus.OK)
        item = PriceItem("A", "Товар", "шт", Decimal("100.00"))
        quote = price_quote([line], {"A": item}, vat_rate=Decimal("0"),
                            delivery=Decimal("50"))
        self.assertEqual(quote.vat, Decimal("0.00"))
        self.assertEqual(quote.total, Decimal("350.00"))


class DocumentConsistencyTest(unittest.TestCase):
    def _quote_dict(self, quote) -> dict:
        return {
            "lines": [
                {
                    "sku": line.sku, "name": line.name, "unit": line.unit,
                    "qty": str(line.qty), "price": str(line.price),
                    "amount": str(line.amount), "net": str(line.net), "vat": str(line.vat),
                }
                for line in quote.lines
            ],
            "subtotal": str(quote.subtotal), "vat": str(quote.vat),
            "delivery": str(quote.delivery), "total": str(quote.total),
            "vat_rate": str(quote.vat_rate),
            "prices_include_vat": int(quote.prices_include_vat),
        }

    def test_excel_sums_match_quote(self) -> None:
        from io import BytesIO

        from openpyxl import load_workbook

        line = RfqLine(idx=1, raw_text="x", qty=Decimal("3"), unit="шт",
                       matched_sku="A", status=LineStatus.OK)
        item = PriceItem("A", "Товар", "шт", Decimal("122.00"))
        quote = price_quote([line], {"A": item}, vat_rate=Decimal("0.22"),
                            prices_include_vat=True)
        rfq = {"id": "a" * 32, "sender": "a@b.ru", "subject": "s", "lines": []}
        book = load_workbook(BytesIO(build_rfq_xlsx(rfq, self._quote_dict(quote))))
        calc = book["Расчёт"]
        # строка 10 — первая позиция: сумма без НДС в колонке G
        self.assertEqual(Decimal(str(calc["G10"].value)), quote.subtotal)
        one_c = book["Для 1С"]
        self.assertEqual(Decimal(str(one_c["E2"].value)), quote.subtotal)
        self.assertEqual(one_c["G2"].value, "22%")

    def test_delivery_is_in_1c_export(self) -> None:
        from io import BytesIO

        from openpyxl import load_workbook

        line = RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                       matched_sku="A", status=LineStatus.OK)
        quote = price_quote([line], {"A": PriceItem("A", "Товар", "шт", Decimal("100.00"))},
                            vat_rate=Decimal("0.22"), delivery=Decimal("100.00"))
        rfq = {"id": "b" * 32, "sender": "a@b.ru", "subject": "s", "lines": []}
        book = load_workbook(BytesIO(build_rfq_xlsx(rfq, self._quote_dict(quote))))
        one_c = book["Для 1С"]
        rows = [[cell.value for cell in row] for row in one_c.iter_rows(min_row=2)]
        delivery = [row for row in rows if row and row[0] == "DELIVERY"]
        self.assertEqual(len(delivery), 1, "доставка должна попасть в выгрузку 1С")
        self.assertEqual(Decimal(str(delivery[0][4])), quote.delivery)
        total = sum(Decimal(str(row[4])) for row in rows if row and row[4] is not None)
        self.assertEqual(total, quote.subtotal + quote.delivery)

    def test_reply_totals_add_up(self) -> None:
        quote = price_quote(
            [
                RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                        matched_sku="A", status=LineStatus.OK),
                RfqLine(idx=2, raw_text="y", qty=Decimal("2"), unit="шт",
                        matched_sku="B", status=LineStatus.OK),
            ],
            {
                "A": PriceItem("A", "A", "шт", Decimal("100.00")),
                "B": PriceItem("B", "B", "шт", Decimal("50.00")),
            },
            vat_rate=Decimal("0.22"),
            delivery=Decimal("100.00"),
        )
        rows = _reply_totals(quote.subtotal, quote.vat, quote.delivery, quote.total,
                            prices_include_vat=False)
        self.assertIn(f"Итого: {quote.total}".replace(".", ","), " ".join(rows).replace(".", ","))

        inclusive = price_quote(
            [
                RfqLine(idx=1, raw_text="x", qty=Decimal("2"), unit="шт",
                        matched_sku="A", status=LineStatus.OK),
            ],
            {"A": PriceItem("A", "A", "шт", Decimal("122.00"))},
            vat_rate=Decimal("0.22"),
            delivery=Decimal("122.00"),
            prices_include_vat=True,
        )
        rows = _reply_totals(inclusive.subtotal, inclusive.vat, inclusive.delivery,
                            inclusive.total, prices_include_vat=True,
                            delivery_vat=inclusive.delivery_vat)
        joined = " ".join(rows).replace(".", ",")
        self.assertIn("Итого:", joined)
        # 244 (товары с НДС) + 122 (доставка с НДС) = 366
        self.assertIn("366,00", joined)
        self.assertIn("В том числе НДС", joined)


class OneCDocumentMathTest(unittest.TestCase):
    """Счёт в 1С должен повторять снимок КП, а не считать по-своему."""

    def _client(self, captured: dict) -> OneCClient:
        refs = {
            "Catalog_Номенклатура": {"value": [{"Ref_Key": "N1", "Артикул": "A",
                                                "Description": "Товар"}]},
            "Catalog_КлассификаторЕдиницИзмерения": {"value": [{"Ref_Key": "U1",
                                                               "Description": "шт"}]},
            "Catalog_Организации": {"value": [{"Ref_Key": "O1"}]},
            "Catalog_Склады": {"value": [{"Ref_Key": "W1"}]},
            "Catalog_Валюты": {"value": [{"Ref_Key": "C1"}]},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = unquote(str(request.url))
            if request.method == "PATCH":
                captured["patch"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={})
            if request.method == "POST":
                return httpx.Response(201, json={"Ref_Key": "D1"})
            for key, payload in refs.items():
                if key in path:
                    return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"value": []})

        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        return OneCClient("https://1c.example/base", "u", "p", http=http)

    def _check(self, inclusive: bool) -> None:
        line = RfqLine(idx=1, raw_text="x", qty=Decimal("3"), unit="шт",
                       matched_sku="A", status=LineStatus.OK)
        item = PriceItem("A", "Товар", "шт", Decimal("122.00"))
        quote = price_quote([line], {"A": item}, vat_rate=Decimal("0.22"),
                            prices_include_vat=inclusive)
        ql = quote.lines[0]
        captured: dict = {}
        client = self._client(captured)
        client.create_invoice(
            counterparty="C",
            lines=[(ql.sku, str(ql.qty), str(ql.price))],
            vat_rate=quote.vat_rate,
            line_vats=[ql.vat],
            prices_include_vat=inclusive,
        )
        rows = captured["patch"]["Товары"]
        self.assertEqual(Decimal(str(rows[0]["Сумма"])), ql.net)
        self.assertEqual(Decimal(str(rows[0]["СуммаНДС"])), ql.vat)
        self.assertEqual(rows[0]["СтавкаНДС"], "НДС22")
        self.assertEqual(
            Decimal(str(captured["patch"]["СуммаДокумента"])),
            ql.net + ql.vat,
            "СуммаДокумента = сумма без НДС + НДС",
        )
        self.assertEqual(Decimal(str(captured["patch"]["СуммаДокумента"])), quote.total)

    def test_invoice_net_prices(self) -> None:
        self._check(inclusive=False)

    def test_invoice_gross_prices(self) -> None:
        self._check(inclusive=True)


class TimingMetricTest(unittest.TestCase):
    def test_versions_do_not_multiply_rfqs(self) -> None:
        db = TMP / f"timing-{uuid.uuid4().hex[:8]}.sqlite3"
        store = Store(db)
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        rfq_id = "a" * 32
        store.save_rfq(rfq_id=rfq_id, message_id="m1", sender="a@b.ru", subject="s",
                       received_at="2026-11-27T10:00:00+00:00", status="draft_ready",
                       raw_text="x", lines=lines)
        for index, duration in enumerate((1000, 2000, 3000)):
            quote = price_quote(
                lines, {"A": PriceItem("A", "A", "шт", Decimal("10"))},
                vat_rate=Decimal("0.22"),
            )
            store.save_quote(f"q{index}", rfq_id, quote, duration_ms=duration)
        stats = store.timing_stats()
        self.assertEqual(stats["count"], 1, "три версии КП — это одна заявка")
        self.assertEqual(stats["median_seconds"], 1.0, "метрика берёт первый расчёт")
        self.assertEqual(stats["max_seconds"], 1.0)

    def test_report_counts_latest_quote_once(self) -> None:
        db = TMP / f"report-{uuid.uuid4().hex[:8]}.sqlite3"
        store = Store(db)
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        store.save_rfq(rfq_id="b" * 32, message_id="m2", sender="a@b.ru", subject="s",
                       received_at="", status="draft_ready", raw_text="x", lines=lines)
        for index, total in enumerate(("10.00", "20.00")):
            q = price_quote(lines, {"A": PriceItem("A", "A", "шт", Decimal(total))},
                            vat_rate=Decimal("0"))
            store.save_quote(f"r{index}", "b" * 32, q)
        report = store.report(3650)
        self.assertEqual(report["rfqs"], 1)
        self.assertEqual(report["quotes"], 1, "последняя версия КП, а не все")
        self.assertEqual(report["total_sum"], 20.0, "сумма последней версии")


if __name__ == "__main__":
    unittest.main()

"""Разные ставки НДС в одной заявке: 22% и 10% одновременно."""

import support

import io
import json
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import httpx

from kpbot.domain import LineStatus, PriceItem, RfqLine, price_quote
from kpbot.export import build_rfq_xlsx
from kpbot.one_c import OneCClient
from kpbot.pipeline import build_reply_text
from kpbot.pricelist import load_price_items
from kpbot.store import Store

TMP = support.subdir("mixed-vat")


def _lines() -> list[RfqLine]:
    return [
        RfqLine(idx=1, raw_text="уголок", qty=Decimal("1"), unit="шт",
                matched_sku="A", status=LineStatus.OK),
        RfqLine(idx=2, raw_text="продукт", qty=Decimal("1"), unit="шт",
                matched_sku="B", status=LineStatus.OK),
    ]


def _items() -> dict[str, PriceItem]:
    return {
        "A": PriceItem("A", "Уголок", "шт", Decimal("100.00"), vat_rate=Decimal("0.22")),
        "B": PriceItem("B", "Продукт", "шт", Decimal("100.00"), vat_rate=Decimal("0.10")),
    }


class MixedVatTest(unittest.TestCase):
    def test_quote_splits_vat_by_rate(self) -> None:
        quote = price_quote(_lines(), _items(), vat_rate=Decimal("0.22"))
        self.assertEqual(quote.subtotal, Decimal("200.00"))
        self.assertEqual(quote.vat, Decimal("32.00"), "22 + 10")
        self.assertEqual(quote.total, Decimal("232.00"))
        self.assertEqual({line.vat_rate for line in quote.lines}, {Decimal("0.22"), Decimal("0.10")})
        breakdown = {rate: vat for rate, _net, vat in quote.vat_breakdown}
        self.assertEqual(breakdown[Decimal("0.22")], Decimal("22.00"))
        self.assertEqual(breakdown[Decimal("0.10")], Decimal("10.00"))

    def test_store_roundtrip_keeps_rates(self) -> None:
        store = Store(TMP / f"mixed-{uuid.uuid4().hex[:8]}.sqlite3")
        store.save_rfq(rfq_id="a" * 32, message_id="m", sender="a@b.ru", subject="s",
                       received_at="", status="draft_ready", raw_text="x", lines=_lines())
        quote = price_quote(_lines(), _items(), vat_rate=Decimal("0.22"))
        store.save_quote("q", "a" * 32, quote)
        restored = store.get_quote("a" * 32)
        rates = [Decimal(row["vat_rate"]) for row in restored["lines"]]
        self.assertEqual(rates, [Decimal("0.22"), Decimal("0.10")])
        self.assertEqual(Decimal(restored["vat"]), Decimal("32.00"))

    def test_price_list_with_vat_column(self) -> None:
        path = TMP / f"price-{uuid.uuid4().hex[:8]}.csv"
        path.write_text(
            "SKU;Наименование;Ед.;Цена;Ставка НДС\n"
            "A;Уголок;шт;100;22\n"
            "B;Продукт;шт;100;10%\n",
            encoding="utf-8-sig",
        )
        items = {item.sku: item for item in load_price_items(path)}
        self.assertEqual(items["A"].vat_rate, Decimal("0.22"))
        self.assertEqual(items["B"].vat_rate, Decimal("0.10"))

    def test_price_list_with_illegal_vat_is_refused(self) -> None:
        path = TMP / f"bad-{uuid.uuid4().hex[:8]}.csv"
        path.write_text(
            "SKU;Наименование;Ед.;Цена;Ставка НДС\nA;Уголок;шт;100;20\n",
            encoding="utf-8-sig",
        )
        from kpbot.domain import ValidationError

        with self.assertRaises(ValidationError):
            load_price_items(path)

    def test_reply_shows_both_rates(self) -> None:
        from kpbot.mailparse import ParsedEmail

        quote = price_quote(_lines(), _items(), vat_rate=Decimal("0.22"))
        text = build_reply_text(
            ParsedEmail(sender="a@b.ru", subject="s", message_id="m", received_at="", text=""),
            quote, "a" * 32,
        )
        self.assertIn("НДС 22%", text)
        self.assertIn("НДС 10%", text)
        self.assertIn("Итого: 232,00", text)

    def test_1c_gets_line_rates(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = unquote(str(request.url))
            if request.method == "PATCH":
                captured["patch"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={})
            if request.method == "POST":
                return httpx.Response(201, json={"Ref_Key": "D1"})
            if "Catalog_Номенклатура" in path:
                return httpx.Response(200, json={"value": [
                    {"Ref_Key": "N-A", "Артикул": "A", "Description": "A"},
                    {"Ref_Key": "N-B", "Артикул": "B", "Description": "B"},
                ]})
            if "Catalog_Классификатор" in path:
                return httpx.Response(200, json={"value": [{"Ref_Key": "U", "Description": "шт"}]})
            return httpx.Response(200, json={"value": [{"Ref_Key": "X"}]})

        client = OneCClient("https://1c.example/base", "u", "p",
                            http=httpx.Client(transport=httpx.MockTransport(handler), timeout=5))
        quote = price_quote(_lines(), _items(), vat_rate=Decimal("0.22"))
        client.create_invoice(
            counterparty="C",
            lines=[(line.sku, str(line.qty), str(line.price)) for line in quote.lines],
            vat_rate=quote.vat_rate,
            line_vats=[line.vat for line in quote.lines],
            line_rates=[line.vat_rate for line in quote.lines],
            prices_include_vat=False,
        )
        rows = captured["patch"]["Товары"]
        self.assertEqual([row["СтавкаНДС"] for row in rows], ["НДС22", "НДС10"])
        self.assertEqual(Decimal(str(captured["patch"]["СуммаДокумента"])), quote.total)

    def test_excel_has_line_rates(self) -> None:
        from openpyxl import load_workbook

        quote = price_quote(_lines(), _items(), vat_rate=Decimal("0.22"))
        quote_dict = {
            "lines": [
                {"sku": line.sku, "name": line.name, "unit": line.unit,
                 "qty": str(line.qty), "price": str(line.price), "amount": str(line.amount),
                 "net": str(line.net), "vat": str(line.vat), "vat_rate": str(line.vat_rate)}
                for line in quote.lines
            ],
            "subtotal": str(quote.subtotal), "vat": str(quote.vat),
            "delivery": "0", "total": str(quote.total), "vat_rate": str(quote.vat_rate),
            "prices_include_vat": 0,
        }
        book = load_workbook(io.BytesIO(build_rfq_xlsx(
            {"id": "b" * 32, "sender": "a@b.ru", "subject": "s", "lines": []}, quote_dict
        )))
        sheet = book["Для 1С"]
        self.assertEqual(sheet["G2"].value, "22%")
        self.assertEqual(sheet["G3"].value, "10%")


    def test_documents_show_both_rates(self) -> None:
        from docx import Document

        from kpbot.config import Settings
        from kpbot.pipeline import Pipeline

        data_dir = TMP / f"pipeline-{uuid.uuid4().hex[:8]}"
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg = Settings(data_dir=data_dir, api_key="k", ui_password="p",
                       session_secret="s", pdf_enabled=False, llm_enabled=False)
        store = Store(cfg.db_path)
        store.replace_price_items(list(_items().values()))
        pipeline = Pipeline(store, cfg)
        result = pipeline.process_text(
            "уголок — 1 шт\nпродукт — 1 шт", sender="a@b.ru", subject="Заявка"
        )
        self.assertEqual(result.total, "232.00")
        html = Path(result.html_path).read_text(encoding="utf-8")
        self.assertIn("НДС 22%", html)
        self.assertIn("НДС 10%", html)
        docx_text = "\n".join(p.text for p in Document(result.docx_path).paragraphs)
        self.assertIn("НДС 22%", docx_text)
        self.assertIn("НДС 10%", docx_text)


if __name__ == "__main__":
    unittest.main()

"""Регрессия на дефекты единиц измерения и потери письма целиком.

Каждый тест — про конкретную молчаливую поломку:

* «мм» приводился к метрам, «м2» — тоже к метрам, «тыс. шт» — к тоннам:
  количество менялось в 1000 раз, а строка уходила в КП;
* строки с площадью, объёмом, парами и погонными метрами не распознавались
  вообще и пропадали из заявки (в письме есть, в КП нет);
* Message-ID без заголовка считался через hash(bytes) — он рандомизируется
  на каждый процесс, и после рестарта то же письмо шло в очередь вторым;
* количество больше max_qty выбрасывало всё письмо целиком.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import hashlib
import unittest
from decimal import Decimal

from kpbot.domain import LineStatus, ValidationError
from kpbot.extract import RuleExtractor, canonical_unit, trailing_quantity, unit_multiplier
from kpbot.mailparse import parse_eml
from kpbot.pipeline import Pipeline
from kpbot.pricelist import load_price_items
from kpbot.config import settings
from kpbot.store import Store


class UnitCanonicalTest(unittest.TestCase):
    def test_millimetres_are_not_metres(self) -> None:
        # «100 мм» не должно стать «100 м»: это ошибка в 1000 раз
        self.assertEqual(canonical_unit("мм"), "мм")
        self.assertEqual(canonical_unit("миллиметров"), "мм")

    def test_area_and_volume_are_not_metres(self) -> None:
        for raw in ("м2", "м²", "кв.м", "кв. м", "кв. метров", "квадратных метров"):
            self.assertEqual(canonical_unit(raw), "м2", raw)
        for raw in ("м3", "м³", "куб.м", "куб. метров", "кубических метров"):
            self.assertEqual(canonical_unit(raw), "м3", raw)

    def test_thousands_are_not_tonnes(self) -> None:
        self.assertEqual(canonical_unit("тыс. шт"), "шт")
        self.assertEqual(canonical_unit("тыс. шт."), "шт")
        self.assertEqual(canonical_unit("т"), "т")

    def test_linear_metre_forms(self) -> None:
        for raw in ("пог.м", "погонных метров", "пог. метров", "м.п.", "п.м.", "пм"):
            self.assertEqual(canonical_unit(raw), "м", raw)

    def test_word_forms(self) -> None:
        self.assertEqual(canonical_unit("тонн"), "т")
        self.assertEqual(canonical_unit("литров"), "л")
        self.assertEqual(canonical_unit("килограмм"), "кг")
        self.assertEqual(canonical_unit("мешков"), "меш")
        self.assertEqual(canonical_unit("листов"), "лист")

    def test_multiplier(self) -> None:
        self.assertEqual(unit_multiplier("тыс. шт"), 1000)
        self.assertEqual(unit_multiplier("шт"), 1)


class ExtractorUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = RuleExtractor()

    def _one(self, text: str):
        lines = self.extractor.extract(text)
        self.assertEqual(len(lines), 1, f"ожидалась одна строка, получено {lines}")
        return lines[0]

    def test_area_is_kept(self) -> None:
        line = self._one("плитка керамическая 100 м2")
        self.assertEqual(str(line.qty), "100.000")
        self.assertEqual(line.unit, "м2")

    def test_volume_is_kept(self) -> None:
        line = self._one("брус 100х100 2 куб.м")
        self.assertEqual(str(line.qty), "2.000")
        self.assertEqual(line.unit, "м3")

    def test_pairs_are_kept(self) -> None:
        line = self._one("плинтус 20 пар")
        self.assertEqual(str(line.qty), "20.000")
        self.assertEqual(line.unit, "пар")

    def test_linear_metres_are_kept(self) -> None:
        line = self._one("провод 50 пог. метров")
        self.assertEqual(str(line.qty), "50.000")
        self.assertEqual(line.unit, "м")

    def test_word_forms_are_kept(self) -> None:
        for text, qty, unit in (
            ("щебень 10 тонн", "10.000", "т"),
            ("краска 20 литров", "20.000", "л"),
            ("саморез 5 килограмм", "5.000", "кг"),
        ):
            line = self._one(text)
            self.assertEqual(str(line.qty), qty, text)
            self.assertEqual(line.unit, unit, text)

    def test_thousands_multiplier(self) -> None:
        # «10 тыс. шт» — это 10 000 штук, а не 10 тонн и не 10 штук
        line = self._one("провод 10 тыс. шт")
        self.assertEqual(str(line.qty), "10000.000")
        self.assertEqual(line.unit, "шт")

    def test_size_in_millimetres_does_not_become_quantity(self) -> None:
        # «12.5 мм» — размер, количество — 20 листов
        line = self._one("гипсокартон 12.5 мм 20 листов")
        self.assertEqual(str(line.qty), "20.000")
        self.assertEqual(line.unit, "лист")


class PriceListUnitTest(unittest.TestCase):
    def test_price_unit_is_canonical(self) -> None:
        path = support.subdir("price-units") / "price.csv"
        path.write_text(
            "SKU;Наименование;Ед.;Цена\n"
            "A-1;Плитка керамическая;кв.м;500\n"
            "A-2;Провод ПуГВ;пог.м;52\n",
            encoding="utf-8-sig",
        )
        items = {item.sku: item for item in load_price_items(path)}
        self.assertEqual(items["A-1"].unit, "м2")
        self.assertEqual(items["A-2"].unit, "м")


class PriceListVatColumnTest(unittest.TestCase):
    """Колонка «Цена с НДС» и валюта прайса (аудит, дефект 22)."""

    def _csv(self, text: str, name: str):
        path = support.subdir("price-vat") / name
        path.write_text(text, encoding="utf-8-sig")
        return path

    def test_vat_column_refused_without_inclusive_mode(self) -> None:
        path = self._csv("Код;Наименование;Ед.;Цена с НДС\nA-1;Уголок;шт;42\n", "vat.csv")
        with self.assertRaises(ValidationError) as ctx:
            load_price_items(path)
        self.assertIn("KPBOT_PRICES_INCLUDE_VAT", str(ctx.exception))

    def test_vat_column_accepted_in_inclusive_mode(self) -> None:
        path = self._csv("Код;Наименование;Ед.;Цена с НДС\nA-1;Уголок;шт;42\n", "vat2.csv")
        items = load_price_items(path, prices_include_vat=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, Decimal("42"))

    def test_column_without_vat_wins(self) -> None:
        # раньше бралась первая по порядку — обычно «с НДС» и НДС начислялся второй раз
        path = self._csv(
            "Код;Наименование;Ед.;Цена с НДС;Цена без НДС\nA-1;Уголок;шт;42.00;34.43\n",
            "both.csv",
        )
        items = load_price_items(path)
        self.assertEqual(items[0].price, Decimal("34.43"))

    def test_plain_price_column_still_works(self) -> None:
        path = self._csv("SKU;Наименование;Ед.;Цена\nA-1;Уголок;шт;34.50\n", "plain.csv")
        self.assertEqual(load_price_items(path)[0].price, Decimal("34.50"))

    def test_foreign_currency_is_refused(self) -> None:
        path = self._csv("Код;Наименование;Ед.;Цена;Валюта\nA-1;Уголок;шт;10;USD\n", "usd.csv")
        with self.assertRaises(ValidationError) as ctx:
            load_price_items(path)
        self.assertIn("USD", str(ctx.exception))

    def test_ruble_column_is_kept(self) -> None:
        path = self._csv(
            "Код;Наименование;Ед.;Цена;Валюта\nA-1;Уголок;шт;10;руб.\nA-2;Кабель;м;20;RUB\n",
            "rub.csv",
        )
        items = load_price_items(path)
        self.assertEqual([item.currency for item in items], ["RUB", "RUB"])


class QuantityWithoutUnitTest(unittest.TestCase):
    """«Подрозетник 68 мм — 20»: количество есть, единицы нет (аудит, дефект 19)."""

    def test_trailing_quantity(self) -> None:
        self.assertEqual(trailing_quantity("Подрозетник 68 мм — 20"), Decimal("20.000"))
        self.assertEqual(trailing_quantity("Уголок 50х50 200"), Decimal("200.000"))
        self.assertEqual(trailing_quantity("труба 25.4 — 1.5"), Decimal("1.500"))

    def test_size_is_not_a_quantity(self) -> None:
        self.assertIsNone(trailing_quantity("труба 25.4"))
        self.assertIsNone(trailing_quantity("кабель ВВГ 3х2.5"))
        self.assertIsNone(trailing_quantity("уголок 50х50"))

    def test_single_position_without_unit_is_not_lost(self) -> None:
        store = Store(support.ROOT / f"nounit-{id(self)}.sqlite3")
        pipeline = Pipeline(store, settings)
        result = pipeline.process_text(
            "Уголок монтажный 50х50 200", sender="a@b.ru", subject="Заявка", message_id="nounit-1"
        )
        lines = store.get_rfq(result.rfq_id)["lines"]
        self.assertEqual(len(lines), 1, "письмо не должно считаться рассылкой")
        self.assertEqual(Decimal(lines[0]["qty"]), Decimal("200"))
        self.assertEqual(lines[0]["unit"], "шт", "единица подставлена из прайса")
        self.assertEqual(lines[0]["status"], LineStatus.AMBIGUOUS.value, "нужно подтверждение")


class MessageIdTest(unittest.TestCase):
    def test_generated_id_is_deterministic(self) -> None:
        raw = (
            b"From: client@example.ru\r\n"
            b"Subject: \xd0\x97\xd0\xb0\xd1\x8f\xd0\xb2\xd0\xba\xd0\xb0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"\xd1\x83\xd0\xb3\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xba 50\xd1\x8550 \xe2\x80\x94 10 \xd1\x88\xd1\x82\r\n"
        )
        first = parse_eml(raw).message_id
        second = parse_eml(raw).message_id
        self.assertEqual(first, second)
        # стабильно между процессами: sha256, а не hash(), который рандомизируется
        self.assertEqual(first, f"local-{hashlib.sha256(raw).hexdigest()[:32]}")


class OversizedQuantityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"oversized-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)

    def test_huge_quantity_does_not_lose_the_whole_letter(self) -> None:
        result = self.pipeline.process_text(
            "уголок 50х50 — 10 шт\nкабель ВВГнг-LS 3х2.5 — 2000000 м",
            sender="a@b.ru",
            subject="Заявка",
            message_id="oversized-1",
        )
        lines = self.store.get_rfq(result.rfq_id)["lines"]
        self.assertEqual(len(lines), 2, "письмо должно сохраниться целиком")
        huge = [line for line in lines if line["qty"] and Decimal(line["qty"]) > 1000000]
        self.assertEqual(len(huge), 1)
        self.assertEqual(huge[0]["status"], LineStatus.INVALID.value)
        # в расчёт попадает только нормальная строка
        quote = self.store.get_quote(result.rfq_id)
        self.assertEqual(len(quote["lines"]), 1)
        self.assertEqual(quote["lines"][0]["sku"], "KR-001")


if __name__ == "__main__":
    unittest.main()

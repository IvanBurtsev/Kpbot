import support  # общий стенд: изолированные данные, сеть запрещена

import unittest
from decimal import Decimal

from kpbot.domain import (
    LineStatus,
    PriceItem,
    Quote,
    RfqLine,
    RfqStatus,
    ValidationError,
    build_reply_subject,
    money,
    price_quote,
    status_for,
)


def item(sku: str, price: str, unit: str = "шт") -> PriceItem:
    return PriceItem(sku=sku, name=f"Позиция {sku}", unit=unit, price=Decimal(price))


class MoneyTest(unittest.TestCase):
    def test_rounds_half_up(self) -> None:
        self.assertEqual(money("1.005"), Decimal("1.01"))
        self.assertEqual(money(Decimal("2.344")), Decimal("2.34"))

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ValidationError):
            money("двадцать рублей")


class PriceQuoteTest(unittest.TestCase):
    def test_prices_only_confirmed_lines(self) -> None:
        lines = [
            RfqLine(idx=1, raw_text="уголок", qty=Decimal("2"), unit="шт",
                    matched_sku="A", status=LineStatus.OK),
            RfqLine(idx=2, raw_text="подрозетник", qty=Decimal("5"), unit="шт",
                    status=LineStatus.NO_MATCH),
        ]
        quote = price_quote(lines, {"A": item("A", "100.00")}, vat_rate=Decimal("0.20"))

        self.assertEqual(len(quote.lines), 1)
        self.assertEqual(quote.subtotal, Decimal("200.00"))
        self.assertEqual(quote.vat, Decimal("40.00"))
        self.assertEqual(quote.total, Decimal("240.00"))
        self.assertTrue(quote.requires_review)
        self.assertEqual(quote.unmatched, 1)
        self.assertEqual(quote.excluded, ["подрозетник"])

    def test_never_invents_price_for_missing_item(self) -> None:
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="GONE", status=LineStatus.OK)]
        quote = price_quote(lines, {}, vat_rate=Decimal("0.20"))
        self.assertEqual(quote.lines, [])
        self.assertEqual(quote.total, Decimal("0.00"))
        self.assertTrue(quote.requires_review)

    def test_zero_qty_line_is_excluded_not_fatal(self) -> None:
        """«0 шт» — битая строка, а не повод выбросить всё письмо."""
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("0"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item("A", "10")})
        self.assertEqual(quote.lines, [])
        self.assertEqual(quote.total, Decimal("0.00"))
        self.assertIn("x", quote.excluded)

    def test_rejects_bad_vat(self) -> None:
        with self.assertRaises(ValidationError):
            price_quote([], {}, vat_rate=Decimal("1.5"))

    def test_delivery_is_taxed_like_goods(self) -> None:
        # доставка — услуга, НДС по той же ставке: 1000 + 500 = 1500 без НДС,
        # НДС 300, итого 1800. Раньше НДС на доставку не начислялся вовсе.
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item("A", "1000")},
                            vat_rate=Decimal("0.20"), delivery=Decimal("500"))
        self.assertEqual(quote.subtotal, Decimal("1000.00"))
        self.assertEqual(quote.delivery, Decimal("500.00"))
        self.assertEqual(quote.delivery_vat, Decimal("100.00"))
        self.assertEqual(quote.vat, Decimal("300.00"))
        self.assertEqual(quote.total, Decimal("1800.00"))
        # «Итого» обязано сходиться с тем, что видит клиент в письме
        self.assertEqual(quote.total, quote.subtotal + quote.vat + quote.delivery)

    def test_delivery_in_inclusive_mode(self) -> None:
        # цены уже с НДС: доставка по тому же правилу — её цена тоже с НДС
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item("A", "1200")},
                            vat_rate=Decimal("0.20"), delivery=Decimal("600"),
                            prices_include_vat=True)
        self.assertEqual(quote.subtotal, Decimal("1000.00"))
        self.assertEqual(quote.delivery, Decimal("600.00"))
        self.assertEqual(quote.delivery_vat, Decimal("100.00"))
        self.assertEqual(quote.total, Decimal("1800.00"), "итого = товары + доставка, без второго НДС")
        self.assertEqual(quote.vat, Decimal("300.00"))
        # НДС уже внутри цен: итого = товары с НДС + доставка с НДС
        self.assertEqual(
            quote.subtotal + quote.vat - quote.delivery_vat + quote.delivery, quote.total
        )

    def test_no_delivery_keeps_old_numbers(self) -> None:
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item("A", "1000")}, vat_rate=Decimal("0.20"))
        self.assertEqual(quote.delivery_vat, Decimal("0.00"))
        self.assertEqual(quote.total, Decimal("1200.00"))

    def test_ambiguous_line_is_excluded(self) -> None:
        lines = [RfqLine(idx=1, raw_text="x", qty=Decimal("1"), unit="шт",
                         matched_sku="A", status=LineStatus.AMBIGUOUS)]
        quote = price_quote(lines, {"A": item("A", "10")})
        self.assertEqual(quote.lines, [])
        self.assertTrue(quote.requires_review)


class StatusTest(unittest.TestCase):
    def test_review_when_unmatched(self) -> None:
        quote = Quote(lines=[], subtotal=Decimal("0"), vat=Decimal("0"),
                      delivery=Decimal("0"), total=Decimal("0"), vat_rate=Decimal("0.2"),
                      requires_review=True, unmatched=2)
        self.assertIs(status_for(quote), RfqStatus.HUMAN_REVIEW)

    def test_reply_subject_does_not_duplicate(self) -> None:
        self.assertEqual(build_reply_subject("Заявка"), "Re: Заявка")
        self.assertEqual(build_reply_subject("re: Заявка"), "re: Заявка")


if __name__ == "__main__":
    unittest.main()

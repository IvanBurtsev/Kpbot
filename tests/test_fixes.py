"""Регрессия на исправления 26.11.2026: деньги, матчинг, документы, доступ.

Каждый тест здесь — про конкретный баг, который уже случался: тысячи через
пробел, «0 шт», количество после тире, обучение без проверки единиц,
подстрочное сравнение размеров, дата письма в КП, вход общим паролем.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import shutil
import unittest
from decimal import Decimal
from pathlib import Path

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.config import Settings  # noqa: E402
from kpbot.domain import LineStatus, PriceItem, Quote, QuoteLine, RfqLine, price_quote  # noqa: E402
from kpbot.extract import RuleExtractor  # noqa: E402
from kpbot.llm import LlmAnswer, LlmExtractor, LlmLine, LlmPayload, LlmUsage  # noqa: E402
from kpbot.matching import PriceIndex, _numbers_bonus, match_lines  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.render import render_template  # noqa: E402
from kpbot.store import Store  # noqa: E402
from kpbot.web import app  # noqa: E402

TMP = Path(support.subdir("fixes"))


def settings(**kwargs) -> Settings:
    base = dict(
        data_dir=TMP,
        api_key="test-key",
        ui_password="test-pass",
        session_secret="test-secret",
        pdf_enabled=False,
        llm_enabled=False,
    )
    base.update(kwargs)
    return Settings(**base)


class ParsingFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = RuleExtractor()

    def _one(self, text: str):
        lines = self.extractor.extract(text)
        self.assertTrue(lines, f"не разобралось: {text!r}")
        return lines[0]

    def test_thousands_with_space(self) -> None:
        self.assertEqual(self._one("кабель 3х2.5 — 1 500 м").qty, Decimal("1500.000"))
        self.assertEqual(self._one("уголок 50х50 — 10 000 шт").qty, Decimal("10000.000"))

    def test_size_and_qty_are_not_glued(self) -> None:
        # «50х50 200» — это размер и количество, а не число 50200
        line = self._one("уголок 50х50 200 шт")
        self.assertEqual(line.qty, Decimal("200.000"))
        self.assertIn("50х50", line.raw_text)

    def test_quantity_after_dash_wins(self) -> None:
        # «25 кг» — характеристика товара, количество — «10 меш» после тире
        line = self._one("шпаклевка финишная 25 кг — 10 меш")
        self.assertEqual(line.qty, Decimal("10.000"))
        self.assertEqual(line.unit, "меш")

    def test_zero_quantity_is_skipped_not_fatal(self) -> None:
        # строка с нулём не становится позицией, но письмо не падает
        self.assertEqual(self.extractor.extract("уголок 50х50 — 0 шт"), [])


class MoneyFixesTest(unittest.TestCase):
    def test_amount_is_computed_from_rounded_price(self) -> None:
        item = PriceItem(sku="A", name="Гайка", unit="шт", price=Decimal("0.335"))
        lines = [RfqLine(idx=1, raw_text="гайка", qty=Decimal("3"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item}, vat_rate=Decimal("0.22"))
        line = quote.lines[0]
        self.assertEqual(line.price, Decimal("0.34"))
        self.assertEqual(line.amount, line.price * line.qty)

    def test_zero_qty_line_does_not_break_quote(self) -> None:
        item = PriceItem(sku="A", name="Гайка", unit="шт", price=Decimal("10"))
        lines = [RfqLine(idx=1, raw_text="гайка", qty=Decimal("0"), unit="шт",
                         matched_sku="A", status=LineStatus.OK)]
        quote = price_quote(lines, {"A": item}, vat_rate=Decimal("0.22"))
        self.assertEqual(quote.lines, [])
        self.assertTrue(quote.requires_review)

    def test_default_vat_is_22_percent(self) -> None:
        self.assertEqual(Settings().vat_rate, Decimal("0.22"))


class MatchingFixesTest(unittest.TestCase):
    def test_numbers_compare_as_whole_tokens(self) -> None:
        self.assertEqual(_numbers_bonus("кабель 4.2", "кабель 14.2"), 0.0)
        self.assertEqual(_numbers_bonus("кабель 14.2", "кабель 14.2"), 1.0)

    def test_item_without_size_is_ambiguous(self) -> None:
        items = [
            PriceItem(sku="EL-003", name="Кабель ВВГнг 5х6", unit="м", price=Decimal("402")),
            PriceItem(sku="EL-001", name="Кабель ВВГнг 3х2.5", unit="м", price=Decimal("128")),
        ]
        lines = match_lines([RfqLine(idx=1, raw_text="кабель", qty=Decimal("10"), unit="м")], items)
        self.assertEqual(lines[0].status, LineStatus.AMBIGUOUS)
        self.assertIn("размер", lines[0].note)

    def test_equal_scores_are_ambiguous(self) -> None:
        items = [
            PriceItem(sku="A", name="Уголок монтажный 50х50", unit="шт", price=Decimal("10")),
            PriceItem(sku="B", name="Уголок монтажный 50х50", unit="шт", price=Decimal("12")),
        ]
        index = PriceIndex(items)
        item, score, tied = index.best("уголок монтажный 50х50", detect_tie=True)
        self.assertTrue(tied)
        self.assertEqual(score, 1.0)
        lines = match_lines(
            [RfqLine(idx=1, raw_text="уголок монтажный 50х50", qty=Decimal("2"), unit="шт")], index
        )
        self.assertEqual(lines[0].status, LineStatus.AMBIGUOUS)


class LearnedFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP / "learned", ignore_errors=True)
        self.settings = settings(data_dir=TMP / "learned")
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)
        self.pipeline.ensure_price_list()

    def test_learned_match_does_not_override_unit(self) -> None:
        # менеджер когда-то подтвердил «уголок» как кабель (единица — метр)
        key = self.pipeline.learn_key("уголок монтажный 50х50 — 10 шт")
        self.store.learn_match(key, "EL-001")
        lines, _, _ = self.pipeline.analyze("уголок монтажный 50х50 — 100 шт")
        self.assertEqual(len(lines), 1)
        # выученная связь с другой единицей не применяется: цена за метр × 100 шт
        self.assertNotEqual(lines[0].matched_sku, "EL-001")
        self.assertEqual(lines[0].status, LineStatus.OK)
        self.assertEqual(lines[0].matched_sku, "KR-001")


class DocumentFixesTest(unittest.TestCase):
    def test_kp_has_number_and_human_date(self) -> None:
        quote = Quote(
            lines=[QuoteLine(sku="KR-001", name="Уголок", unit="шт", qty=Decimal("2"),
                             price=Decimal("34.50"), amount=Decimal("69.00"))],
            subtotal=Decimal("69.00"),
            vat=Decimal("15.18"),
            delivery=Decimal("0.00"),
            total=Decimal("84.18"),
            vat_rate=Decimal("0.22"),
            requires_review=False,
            unmatched=0,
            number=12,
        )
        html = render_template(
            "kp.html",
            company={"name": "ООО Тест", "inn": "1", "kpp": "2", "address": "a",
                     "phone": "p", "email": "e", "signer": "s", "logo": "", "kp_intro": ""},
            client={"sender": "a@b.ru", "subject": "Заявка"},
            rfq={"id": "0123456789abcdef0123456789abcdef",
                 "received_at": "Fri, 20 Nov 2026 10:00:00 +0300"},
            quote=quote,
            lines=[],
            valid_days=7,
        )
        self.assertIn("№ 12", html)
        self.assertIn("20.11.2026", html)
        self.assertNotIn("Fri", html)
        # цены без НДС, НДС сверху — текст не должен утверждать обратное
        self.assertIn("указаны без НДС", html)
        self.assertIn("22%", html)

    def test_quantity_filter_has_no_trailing_zeros(self) -> None:
        from kpbot.render import _qty

        self.assertEqual(_qty(Decimal("10.000")), "10")
        self.assertEqual(_qty(Decimal("2.500")), "2,5")


class LoggingInProdTest(unittest.TestCase):
    def test_shared_login_is_off_in_production(self) -> None:
        self.assertFalse(Settings(env="prod", allow_shared_login=False).shared_login_allowed)
        self.assertTrue(Settings(env="dev").shared_login_allowed)


class AuthzTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": "test-pass"})

    def test_foreign_or_unknown_rfq_is_404(self) -> None:
        unknown = "f" * 32
        response = self.client.get(f"/ui/rfq/{unknown}/kp.html")
        self.assertEqual(response.status_code, 404)


class LlmGroundingTest(unittest.TestCase):
    def test_invented_line_is_dropped(self) -> None:
        class FakeClient:
            def complete(self, text: str) -> LlmAnswer:
                return LlmAnswer(
                    payload=LlmPayload(lines=[
                        LlmLine(name="уголок 50х50", qty="10", unit="шт"),
                        LlmLine(name="кабель ВВГнг 5х6", qty="1000", unit="м"),
                    ]),
                    usage=LlmUsage(prompt_tokens=1, completion_tokens=1),
                )

        extractor = LlmExtractor(FakeClient())
        lines = extractor.extract("уголок 50х50 — 10 шт")
        texts = [line.raw_text for line in lines]
        self.assertTrue(any("уголок" in text for text in texts), texts)
        self.assertFalse(any("кабель" in text for text in texts), texts)

    def test_grounding_accepts_word_forms(self) -> None:
        """«уголок» в ответе модели и «уголков» в письме — одно и то же слово."""
        class FakeClient:
            def complete(self, text: str) -> LlmAnswer:
                return LlmAnswer(
                    payload=LlmPayload(lines=[LlmLine(name="уголок 50х50", qty="10", unit="шт")]),
                    usage=LlmUsage(),
                )

        extractor = LlmExtractor(FakeClient())
        lines = extractor.extract("нужен десяток уголков 50х50")
        self.assertTrue(any("уголок" in line.raw_text for line in lines), lines)


if __name__ == "__main__":
    unittest.main()

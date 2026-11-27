"""Ставка НДС и режим цен — на каждого клиента, а не на сервис.

У поставщиков разные законные ставки: 22% (основная с 01.01.2026), 10% для
отдельных товаров, 0/5/7% для спецрежимов. 20% — ставка прошлых лет, она не
должна попадать в документы 2026 года.
Одна ставка на процесс делает такой набор клиентов невозможным: второй клиент
получает неверный налог.

Здесь проверяется:

* настройки клиента перекрывают `.env`, а битое значение не меняет деньги молча;
* ставка, режим цен и доставка доходят до расчёта и до документов;
* интерфейс сохраняет ставку в процентах, а хранит долей;
* сервис не стартует с `KPBOT_VAT_RATE=22` (это 2200%, а не 22%).
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError

from kpbot.config import Settings
from kpbot.domain import CalcSettings, LineStatus, PriceItem, RfqLine, price_quote
from kpbot.pipeline import Pipeline, calc_settings
from kpbot.store import Store


def _settings(data_dir, **kwargs) -> Settings:
    base = dict(
        data_dir=data_dir,
        api_key="test-key",
        ui_password="test-pass",
        session_secret="test-secret",
        pdf_enabled=False,
    )
    base.update(kwargs)
    return Settings(**base)


def _one_line(sku: str = "KR-001", qty: str = "10") -> list[RfqLine]:
    return [RfqLine(idx=1, raw_text="уголок 50х50", qty=Decimal(qty), unit="шт",
                    matched_sku=sku, status=LineStatus.OK)]


class StartupValidationTest(unittest.TestCase):
    """Опечатка в .env должна ловиться при запуске, а не на первом письме."""

    def test_rate_as_percent_is_rejected(self) -> None:
        with self.assertRaises(PydanticValidationError) as ctx:
            _settings(support.ROOT, vat_rate=Decimal("22"))
        self.assertIn("0.22", str(ctx.exception))

    def test_normal_rate_passes(self) -> None:
        self.assertEqual(_settings(support.ROOT, vat_rate=Decimal("0.22")).vat_rate, Decimal("0.22"))
        self.assertEqual(_settings(support.ROOT, vat_rate=Decimal("0.10")).vat_rate, Decimal("0.10"))
        self.assertEqual(_settings(support.ROOT, vat_rate=Decimal("0")).vat_rate, Decimal("0"))
        # 20% не запрещена на уровне модели (исторические данные читаются),
        # но в расчёт и в прод она не проходит — см. тесты ниже.

    def test_negative_delivery_is_rejected(self) -> None:
        with self.assertRaises(PydanticValidationError):
            _settings(support.ROOT, delivery_price=Decimal("-1"))


class TenantCalcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"tenant-calc-{id(self)}.sqlite3")
        self.cfg = _settings(support.ROOT)
        self.pipeline = Pipeline(self.store, self.cfg)

    def test_defaults_come_from_server(self) -> None:
        calc = calc_settings(self.store, self.cfg)
        self.assertEqual(calc.vat_rate, Decimal("0.22"))
        self.assertFalse(calc.prices_include_vat)
        self.assertEqual(calc.delivery_price, Decimal("0.00"))

    def test_tenant_rate_overrides_server(self) -> None:
        self.store.set_settings({"vat_rate": "0.10"})
        self.assertEqual(calc_settings(self.store, self.cfg).vat_rate, Decimal("0.10"))

    def test_illegal_tenant_rate_falls_back_to_22(self) -> None:
        # 20% — ставка до 2026 года: в документы 2026-го она попасть не должна
        self.store.set_settings({"vat_rate": "0.20"})
        self.assertEqual(calc_settings(self.store, self.cfg).vat_rate, Decimal("0.22"))

    def test_tenant_mode_and_delivery(self) -> None:
        self.store.set_settings({"prices_include_vat": "1", "delivery_price": "500"})
        calc = calc_settings(self.store, self.cfg)
        self.assertTrue(calc.prices_include_vat)
        self.assertEqual(calc.delivery_price, Decimal("500"))

    def test_broken_value_falls_back_instead_of_breaking_money(self) -> None:
        self.store.set_settings({"vat_rate": "двадцать два", "delivery_price": "-5"})
        calc = calc_settings(self.store, self.cfg)
        self.assertEqual(calc.vat_rate, Decimal("0.22"), "мусор не должен менять налог")
        self.assertEqual(calc.delivery_price, Decimal("0.00"))

    def test_rate_out_of_range_falls_back(self) -> None:
        self.store.set_settings({"vat_rate": "22"})
        self.assertEqual(calc_settings(self.store, self.cfg).vat_rate, Decimal("0.22"))


class TenantQuoteTest(unittest.TestCase):
    """Ставка клиента доходит до денег, а не только до настроек."""

    def test_ten_percent_tenant_counts_ten(self) -> None:
        store = Store(support.ROOT / f"tenant-quote-10-{id(self)}.sqlite3")
        cfg = _settings(support.ROOT)
        pipeline = Pipeline(store, cfg)
        store.set_settings({"vat_rate": "0.10"})
        quote = price_quote(
            _one_line(), {"KR-001": PriceItem("KR-001", "Уголок", "шт", Decimal("34.50"))},
            **pipeline.calc().as_kwargs(),
        )
        self.assertEqual(quote.subtotal, Decimal("345.00"))
        self.assertEqual(quote.vat, Decimal("34.50"), "10%: 345 × 0.1")
        self.assertEqual(quote.total, Decimal("379.50"))
        self.assertEqual(quote.vat_rate, Decimal("0.10"))

    def test_illegal_twenty_percent_tenant_counts_22(self) -> None:
        store = Store(support.ROOT / f"tenant-quote-20-{id(self)}.sqlite3")
        cfg = _settings(support.ROOT)
        pipeline = Pipeline(store, cfg)
        store.set_settings({"vat_rate": "0.20"})
        quote = price_quote(
            _one_line(), {"KR-001": PriceItem("KR-001", "Уголок", "шт", Decimal("34.50"))},
            **pipeline.calc().as_kwargs(),
        )
        self.assertEqual(quote.vat, Decimal("75.90"), "20% заменяется законными 22%")
        self.assertEqual(quote.vat_rate, Decimal("0.22"))

    def test_twenty_two_percent_tenant_counts_twenty_two(self) -> None:
        store = Store(support.ROOT / f"tenant-quote-22-{id(self)}.sqlite3")
        cfg = _settings(support.ROOT)
        pipeline = Pipeline(store, cfg)
        store.set_settings({"vat_rate": "0.22"})
        quote = price_quote(
            _one_line(), {"KR-001": PriceItem("KR-001", "Уголок", "шт", Decimal("34.50"))},
            **pipeline.calc().as_kwargs(),
        )
        self.assertEqual(quote.vat, Decimal("75.90"), "22%: 345 × 0.22")
        self.assertEqual(quote.total, Decimal("420.90"))

    def test_tenant_delivery_is_included(self) -> None:
        store = Store(support.ROOT / f"tenant-quote-delivery-{id(self)}.sqlite3")
        pipeline = Pipeline(store, _settings(support.ROOT))
        store.set_settings({"delivery_price": "500"})
        quote = price_quote(
            _one_line(), {"KR-001": PriceItem("KR-001", "Уголок", "шт", Decimal("34.50"))},
            **pipeline.calc().as_kwargs(),
        )
        # 345 + 500 = 845 без НДС, НДС 22% = 185,90, итого 1030,90
        self.assertEqual(quote.delivery, Decimal("500.00"))
        self.assertEqual(quote.total, Decimal("1030.90"))

    def test_calc_settings_as_kwargs_matches_price_quote(self) -> None:
        calc = CalcSettings(vat_rate=Decimal("0.05"), prices_include_vat=True,
                            delivery_price=Decimal("100"))
        kwargs = calc.as_kwargs(max_qty=Decimal("10"))
        self.assertEqual(kwargs["vat_rate"], Decimal("0.05"))
        self.assertEqual(kwargs["delivery"], Decimal("100"))
        self.assertTrue(kwargs["prices_include_vat"])
        self.assertEqual(kwargs["max_qty"], Decimal("10"))


class TenantIsolationTest(unittest.TestCase):
    """Два клиента с разными ставками в одном сервисе — то, чего не было.

    База у каждого своя, а настройка расчёта раньше была общей на процесс:
    второй клиент получал чужой налог.
    """

    def test_two_tenants_two_rates(self) -> None:
        import uuid
        from pathlib import Path

        from kpbot.tenancy import TenantRegistry

        run = uuid.uuid4().hex[:8]
        main_db = support.ROOT / f"iso-main-{run}.sqlite3"
        registry = TenantRegistry(support.ROOT / f"iso-reg-{run}.sqlite3", main_db)
        registry.ensure_default()
        old_db = Path(main_db).parent / f"iso-old-{run}.sqlite3"
        registry.create("Льготная база 10%", old_db)

        cfg = _settings(support.ROOT)
        stores = {tenant["id"]: Store(Path(tenant["db_path"])) for tenant in registry.list()}
        stores["default"].set_settings({"vat_rate": "0.22"})
        old_id = next(tid for tid in stores if tid != "default")
        stores[old_id].set_settings({"vat_rate": "0.10"})

        self.assertEqual(calc_settings(stores["default"], cfg).vat_rate, Decimal("0.22"))
        self.assertEqual(calc_settings(stores[old_id], cfg).vat_rate, Decimal("0.10"))

        # и деньги считаются по своей ставке у каждого: 10 × 100 = 1000 без НДС
        items = {"KR-001": PriceItem("KR-001", "Уголок", "шт", Decimal("100"))}
        for tenant_id, expected_vat in (("default", "220.00"), (old_id, "100.00")):
            pipeline = Pipeline(stores[tenant_id], cfg)
            quote = price_quote(_one_line(), items, **pipeline.calc().as_kwargs())
            self.assertEqual(quote.vat, Decimal(expected_vat), tenant_id)


class SettingsUiTest(unittest.TestCase):
    """Интерфейс вводит проценты, а хранит долю — и не сбрасывает чужие поля."""

    def setUp(self) -> None:
        os.environ.setdefault("KPBOT_API_KEY", "test-key")
        os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
        os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
        from fastapi.testclient import TestClient

        from kpbot.web import app, settings, store

        self.store = store
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def _csrf(self) -> str:
        page = self.client.get("/ui/settings")
        marker = 'name="csrf_token" value="'
        start = page.text.index(marker) + len(marker)
        return page.text[start : page.text.index('"', start)]

    def test_rate_is_saved_as_fraction(self) -> None:
        token = self._csrf()
        response = self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО Тест", "vat_rate": "22", "delivery_price": "500",
                  "prices_include_vat": "1", "vat_mode": "1"},
        )
        self.assertEqual(response.status_code, 303)
        saved = self.store.get_settings()
        self.assertEqual(saved.get("vat_rate"), "0.2200")
        self.assertEqual(saved.get("delivery_price"), "500.00")
        self.assertEqual(saved.get("prices_include_vat"), "1")

    def test_unchecked_box_turns_mode_off(self) -> None:
        token = self._csrf()
        self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО Тест", "vat_rate": "22", "vat_mode": "1"},
        )
        self.assertEqual(self.store.get_settings().get("prices_include_vat"), "0")

    def test_page_shows_rate_in_percent(self) -> None:
        self.store.set_settings({"vat_rate": "0.22"})
        page = self.client.get("/ui/settings")
        self.assertIn('name="vat_rate" value="22"', page.text)

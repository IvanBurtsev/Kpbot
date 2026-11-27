"""Личный кабинет клиента: вход, каталог, корзина, заказы, изоляция, 1С-заглушки."""

import support  # общий стенд: изолированные данные, сеть запрещена

import re
import unittest
import uuid
from decimal import Decimal
from urllib.parse import unquote

from fastapi.testclient import TestClient

from kpbot.cabinet import CLIENT_COOKIE
from kpbot.cabinet_store import CabinetStore
from kpbot.security import csrf_for
from kpbot.store import Store
from kpbot.web import app, pipeline, settings

supplier = Store(settings.db_path)
pipeline.ensure_price_list()
STOCK_SKU = supplier.price_items()[0]
SUPPLIER_CSRF = ""


def csrf_from(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def make_company(admin: bool = True):
    cabinet = CabinetStore(settings.db_path)
    suffix = uuid.uuid4().hex[:8]
    company = cabinet.create_company(f"ООО Тест-{suffix}", inn="7700000000")
    password = "password1"
    user = cabinet.create_user(company["id"], email=f"admin-{suffix}@test.ru",
                               password=password, role="client_admin" if admin else "client_manager")
    return cabinet, company, user, password


class CabinetBase(unittest.TestCase):
    def setUp(self) -> None:
        self.cabinet, self.company, self.user, self.password = make_company()
        self.client = TestClient(app, follow_redirects=False)
        response = self.client.post("/cabinet/login",
                                    data={"email": self.user["email"], "password": self.password})
        self.assertEqual(response.status_code, 303)
        self.csrf = csrf_for(settings.session_secret, self.client.cookies.get(CLIENT_COOKIE))
        self.cabinet.set_pack(STOCK_SKU.sku, 10)
        self.cabinet.set_price(self.company["id"], STOCK_SKU.sku, "1.00")
        supplier.replace_stock([(STOCK_SKU.sku, "100", STOCK_SKU.unit, "Основной склад")])


class LoginTest(CabinetBase):
    def test_wrong_password(self) -> None:
        fresh = TestClient(app, follow_redirects=False)
        response = fresh.post("/cabinet/login",
                              data={"email": self.user["email"], "password": "wrongpass1"})
        self.assertEqual(response.status_code, 401)

    def test_blocked_company_cannot_login(self) -> None:
        self.cabinet.set_company_status(self.company["id"], "blocked")
        fresh = TestClient(app, follow_redirects=False)
        response = fresh.post("/cabinet/login",
                              data={"email": self.user["email"], "password": self.password})
        self.assertEqual(response.status_code, 403)
        self.assertIn("приостановлен", response.text)

    def test_manager_has_no_finance(self) -> None:
        manager, company, user, password = make_company(admin=False)
        fresh = TestClient(app, follow_redirects=False)
        fresh.post("/cabinet/login", data={"email": user["email"], "password": password})
        page = fresh.get("/cabinet")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Кредитный лимит", page.text)
        admin_page = self.client.get("/cabinet")
        self.assertIn("Кредитный лимит", admin_page.text)


class CatalogCartOrderTest(CabinetBase):
    def test_catalog_shows_personal_price_and_pack(self) -> None:
        page = self.client.get("/cabinet/catalog")
        self.assertEqual(page.status_code, 200)
        self.assertIn(STOCK_SKU.sku, page.text)
        self.assertIn("ваша цена", page.text)
        self.assertIn("10", page.text)  # кратность

    def test_cart_rounds_to_pack_and_creates_order(self) -> None:
        self.client.post("/cabinet/cart/add",
                         data={"csrf_token": self.csrf, "sku": STOCK_SKU.sku, "qty": "15"})
        cart = self.client.get("/cabinet/cart")
        self.assertIn("20", cart.text)

        response = self.client.post("/cabinet/orders",
                                    data={"csrf_token": self.csrf, "comment": "доставка до 12"})
        self.assertEqual(response.status_code, 303)
        order_id = response.headers["location"].split("/orders/")[1].split("?")[0]
        order = self.cabinet.order(order_id)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["comment"], "доставка до 12")
        self.assertEqual(Decimal(order["total"]), Decimal("24.40"))  # 20 × 1 ₽ + НДС 22%
        self.assertTrue(order["rfq_id"])
        rfq = supplier.get_rfq(order["rfq_id"])
        self.assertEqual(rfq["status"], "draft_ready")
        self.assertEqual(supplier.get_quote(order["rfq_id"])["total"], "24.40")
        self.assertEqual(self.cabinet.cart_items(self.user["id"]), [])

    def test_stock_limit_is_enforced(self) -> None:
        self.client.post("/cabinet/cart/add",
                         data={"csrf_token": self.csrf, "sku": STOCK_SKU.sku, "qty": "200"})
        before = len(self.cabinet.list_orders(self.company["id"]))
        response = self.client.post("/cabinet/orders", data={"csrf_token": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])
        self.assertIn("на складе", unquote(response.headers["location"]))
        self.assertEqual(len(self.cabinet.list_orders(self.company["id"])), before)

    def test_csrf_required(self) -> None:
        response = self.client.post("/cabinet/cart/add",
                                    data={"sku": STOCK_SKU.sku, "qty": "10"})
        self.assertEqual(response.status_code, 403)


class IsolationTest(CabinetBase):
    def test_other_company_cannot_open_order(self) -> None:
        self.client.post("/cabinet/cart/add",
                         data={"csrf_token": self.csrf, "sku": STOCK_SKU.sku, "qty": "10"})
        created = self.client.post("/cabinet/orders", data={"csrf_token": self.csrf})
        order_id = created.headers["location"].split("/orders/")[1].split("?")[0]

        _, _, other_user, other_password = make_company(admin=False)
        other = TestClient(app, follow_redirects=False)
        other.post("/cabinet/login", data={"email": other_user["email"], "password": other_password})
        page = other.get(f"/cabinet/orders/{order_id}")
        self.assertEqual(page.status_code, 404)
        self.assertNotIn(order_id, page.text)
        self.assertEqual(CabinetStore(settings.db_path).list_orders(other_user["company_id"]), [])


class AdminAndDocsTest(CabinetBase):
    def _order(self) -> str:
        self.client.post("/cabinet/cart/add",
                         data={"csrf_token": self.csrf, "sku": STOCK_SKU.sku, "qty": "10"})
        created = self.client.post("/cabinet/orders", data={"csrf_token": self.csrf})
        return created.headers["location"].split("/orders/")[1].split("?")[0]

    def _supplier(self) -> TestClient:
        client = TestClient(app, follow_redirects=False)
        client.post("/ui/login", data={"password": settings.ui_password})
        return client

    def test_supplier_creates_client_and_orders_visible(self) -> None:
        admin = self._supplier()
        page = admin.get("/ui/admin?tab=clients")
        token = csrf_from(page.text)
        suffix = uuid.uuid4().hex[:6]
        response = admin.post("/ui/admin/clients", data={
            "csrf_token": token,
            "name": f"ООО Новый-{suffix}",
            "email": f"new-{suffix}@test.ru",
            "role": "client_admin",
        })
        # пароль показывается в теле ответа, а не в адресе (чтобы не утёк в логи)
        self.assertEqual(response.status_code, 200)
        self.assertIn("пароль:", response.text)
        password = re.search(r"пароль: (\S+)", response.text).group(1)
        fresh = TestClient(app, follow_redirects=False)
        login = fresh.post("/cabinet/login",
                           data={"email": f"new-{suffix}@test.ru", "password": password})
        self.assertEqual(login.status_code, 303)

        order_id = self._order()
        admin_page = admin.get("/ui/admin?tab=orders")
        self.assertIn(self.cabinet.order(order_id)["number"], admin_page.text)
        self.assertIn("заявка", admin_page.text)

    def test_status_is_visible_to_client(self) -> None:
        order_id = self._order()
        admin = self._supplier()
        token = csrf_from(admin.get("/ui/admin?tab=orders").text)
        response = admin.post(f"/ui/admin/orders/{order_id}/status",
                              data={"csrf_token": token, "status": "shipped"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.cabinet.order(order_id)["status"], "shipped")
        page = self.client.get(f"/cabinet/orders/{order_id}")
        self.assertIn("Отгружен", page.text)

    def test_order_shows_supplier_request_status(self) -> None:
        order_id = self._order()
        page = self.client.get(f"/cabinet/orders/{order_id}")
        self.assertIn("Заявка поставщику", page.text)
        self.assertIn("КП готово", page.text)

    def test_repeat_order_requires_csrf(self) -> None:
        order_id = self._order()
        response = self.client.post(f"/cabinet/orders/{order_id}/repeat")
        self.assertEqual(response.status_code, 403)

    def test_catalog_reports_total_and_page(self) -> None:
        page = self.client.get("/cabinet/catalog")
        self.assertIn("Найдено:", page.text)
        self.assertIn("показаны", page.text)

    def test_documents_and_act(self) -> None:
        order_id = self._order()
        doc = self.client.get(f"/cabinet/orders/{order_id}/doc/invoice")
        self.assertEqual(doc.status_code, 200)
        self.assertIn(self.cabinet.order(order_id)["number"], doc.text)
        self.assertIn("Всего к оплате", doc.text)
        self.assertIn("НДС", doc.text)
        # реквизиты не настроены в тестовой базе — документ обязан сказать это прямо
        self.assertIn("Банковские реквизиты не заполнены", doc.text)

        upd = self.client.get(f"/cabinet/orders/{order_id}/doc/upd")
        self.assertEqual(upd.status_code, 200)
        self.assertIn("Универсальный передаточный документ", upd.text)
        self.assertIn("Главный бухгалтер", upd.text)

        act = self.client.get("/cabinet/documents/act")
        self.assertIn("Акт сверки", act.text)
        self.assertIn("за период с", act.text)
        self.assertIn("Оборот за период", act.text)

        empty = self.client.get("/cabinet/documents/act?start=2035-01-01&end=2035-12-31")
        self.assertIn("за выбранный период заказов нет", empty.text)

        self.cabinet.update_status(order_id, "cancelled")
        cancelled = self.client.get("/cabinet/documents/act")
        self.assertIn("Отменённые заказы", cancelled.text)

        _, _, manager, password = make_company(admin=False)
        fresh = TestClient(app, follow_redirects=False)
        fresh.post("/cabinet/login", data={"email": manager["email"], "password": password})
        self.assertEqual(fresh.get("/cabinet/documents/act").status_code, 403)


class OneCStubTest(CabinetBase):
    def test_stock_and_prices_require_key(self) -> None:
        self.assertEqual(self.client.get("/cabinet/api/1c/stock").status_code, 401)
        ok = self.client.get("/cabinet/api/1c/stock", headers={"X-API-Key": settings.api_key})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(any(row["sku"] == STOCK_SKU.sku for row in ok.json()))
        prices = self.client.get("/cabinet/api/1c/prices", headers={"X-API-Key": settings.api_key})
        self.assertEqual(prices.status_code, 200)

    def test_settlements_update_finance(self) -> None:
        response = self.client.post(
            "/cabinet/api/1c/settlements",
            json={"company_id": self.company["id"], "balance": "1000", "debt": "250",
                  "credit_limit": "5000", "status": "active"},
            headers={"X-API-Key": settings.api_key},
        )
        self.assertEqual(response.status_code, 200)
        company = self.cabinet.company(self.company["id"])
        self.assertEqual(Decimal(company["debt"]), Decimal("250"))
        self.assertEqual(Decimal(company["balance"]), Decimal("1000"))

    def test_order_status_from_1c(self) -> None:
        self.client.post("/cabinet/cart/add",
                         data={"csrf_token": self.csrf, "sku": STOCK_SKU.sku, "qty": "10"})
        created = self.client.post("/cabinet/orders", data={"csrf_token": self.csrf})
        order_id = created.headers["location"].split("/orders/")[1].split("?")[0]
        response = self.client.post(
            f"/cabinet/api/1c/orders/{order_id}/status",
            json={"status": "processing"},
            headers={"X-API-Key": settings.api_key},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cabinet.order(order_id)["status"], "processing")


if __name__ == "__main__":
    unittest.main()

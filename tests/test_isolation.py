"""Изоляция клиентов: набор «красной команды».

Задача продукта — чтобы при 10, 40 и 50 клиентах они не пересеклись ни одним
способом: ни данными, ни документами, ни ценами, ни ключами API, ни сессиями.
Здесь каждый тест пытается перейти границу и обязан получить отказ.

Что проверяется:

1. Ключ API клиента не открывает другого клиента, ключ владельца не открывает
   чужие базы (аудит, дефект 23).
2. Документы (КП, выгрузки) одного клиента не открываются по чужому контексту,
   даже если известен идентификатор заявки, и лежат в отдельной папке.
3. Индекс прайса в памяти не переиспользуется между клиентами — даже когда у
   копии базы совпадают число позиций и время обновления.
4. Сессия сотрудника одного поставщика не действует в базе другого.
5. Сессия покупателя одного кабинета не действует в кабинете другого.
6. Список заявок и статистика другого клиента не видны.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import shutil
import unittest
import uuid
from pathlib import Path

os.environ["KPBOT_API_KEY"] = "owner-key-for-tests"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.config import Settings  # noqa: E402
from kpbot.domain import PriceItem  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402
from kpbot.tenant_ctx import supplier_db  # noqa: E402
from kpbot.web import app, registry, settings  # noqa: E402

_TMP = support.subdir("isolation")


def _make_tenant(prefix: str) -> tuple[dict, Store]:
    code = prefix + uuid.uuid4().hex[:8]
    tenant = registry.create(f"Изоляция {code}", _TMP / f"{code}.sqlite3", tenant_id=code)
    store = Store(Path(tenant["db_path"]))
    store.init_schema()
    return tenant, store


class PriceIndexIsolationTest(unittest.TestCase):
    """Кэш индекса прайса — по клиенту, а не на всех.

    Копия базы клиента (обычный способ завести нового поставщика) даёт одинаковые
    число позиций и время обновления прайса. С общим кэшем второй клиент считал бы
    по ценам первого.
    """

    class _SwitchingStore:
        """Как TenantStore в веб-приложении: база выбирается в момент вызова."""

        def __init__(self, stores: dict[str, Store]) -> None:
            self.stores = stores
            self.current = "a"
            self.db_path = Path(stores["a"].db_path)

        def __getattr__(self, name):
            return getattr(self.stores[self.current], name)

    def test_same_price_meta_does_not_share_index(self) -> None:
        _, store_a = _make_tenant("pi-a-")
        _, store_b = _make_tenant("pi-b-")
        store_a.replace_price_items([PriceItem("S-1", "Кабель ВВГ 3х2.5", "м", 100)])
        store_b.replace_price_items([PriceItem("S-1", "Кабель ВВГ 3х2.5", "м", 500)])
        # ровно тот же снимок прайса, что и у клиента A
        with store_b._connect() as conn, conn:
            conn.execute("UPDATE price_meta SET v = (SELECT v FROM price_meta) WHERE k = 'updated_at'")
        store_b.set_settings({"vat_rate": "0.22"})

        facade = self._SwitchingStore({"a": store_a, "b": store_b})
        pipeline = Pipeline(facade, Settings(data_dir=_TMP, api_key="k", ui_password="p",
                                            session_secret="s", pdf_enabled=False))

        facade.current = "a"
        index_a = pipeline.price_index()
        facade.current = "b"
        facade.db_path = Path(store_b.db_path)
        index_b = pipeline.price_index()

        self.assertEqual(index_a.items[0].price, 100)
        self.assertEqual(index_b.items[0].price, 500, "клиент B считает по своему прайсу")

class FileIsolationTest(unittest.TestCase):
    """Документы клиента лежат в его папке и не отдаются в чужом контексте."""

    def setUp(self) -> None:
        self.tenant_a, self.store_a = _make_tenant("fa-")
        self.tenant_b, self.store_b = _make_tenant("fb-")
        cfg = Settings(data_dir=_TMP, api_key="k", ui_password="p", session_secret="s",
                       pdf_enabled=False)
        self.pipeline_a = Pipeline(self.store_a, cfg)
        self.pipeline_a.ensure_price_list()
        self.result = self.pipeline_a.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Секрет клиента A",
            message_id="iso-a-1",
        )
        self.client = TestClient(app, follow_redirects=False)

    def test_tenant_documents_live_in_own_folder(self) -> None:
        own = self.pipeline_a.out_dir()
        self.assertIn("clients", own.parts, "у клиента своя папка, а не общая data/out")
        self.assertTrue((own / f"{self.result.rfq_id}.html").exists())

    def test_other_tenant_cannot_download_documents(self) -> None:
        # настоящий вход администратором клиента B, затем попытка открыть
        # документы заявки клиента A по её идентификатору
        self.store_b.create_user(email="admin-b@iso.ru", password="strongpass1", role="admin")
        login = self.client.post(
            "/ui/login", data={"email": "admin-b@iso.ru", "password": "strongpass1"}
        )
        self.assertEqual(login.status_code, 303)
        for path in ("kp.html", "kp.pdf", "kp.docx", "export.xlsx"):
            response = self.client.get(f"/ui/rfq/{self.result.rfq_id}/{path}")
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("Секрет клиента A", response.text)

    def test_card_of_other_tenant_is_not_found(self) -> None:
        token = supplier_db.set(Path(self.tenant_b["db_path"]))
        try:
            self.assertIsNone(self.store_b.get_rfq(self.result.rfq_id))
        finally:
            supplier_db.reset(token)


class ApiIsolationTest(unittest.TestCase):
    PAYLOAD = {"sender": "client@example.ru", "subject": "Заявка", "text": "уголок 50х50 — 3 шт"}

    def setUp(self) -> None:
        self.tenant_a, self.store_a = _make_tenant("api-a-")
        self.tenant_b, self.store_b = _make_tenant("api-b-")
        self.client = TestClient(app, follow_redirects=False)

    def test_client_key_opens_only_own_tenant(self) -> None:
        key = registry.issue_key(self.tenant_a["id"])
        ok = self.client.post("/api/v1/rfqs", json=self.PAYLOAD,
                              headers={"X-API-Key": key, "X-Tenant": self.tenant_a["id"]})
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(self.store_a.stats()["rfqs"], 1)

        foreign = self.client.post("/api/v1/rfqs", json=self.PAYLOAD,
                                   headers={"X-API-Key": key, "X-Tenant": self.tenant_b["id"]})
        self.assertEqual(foreign.status_code, 401)
        self.assertEqual(self.store_b.stats()["rfqs"], 0, "в чужую базу ничего не попало")

    def test_owner_key_does_not_reach_clients(self) -> None:
        # ключ владельца адресует только основную базу: утечка одного ключа
        # не должна открывать финансы всех клиентов
        response = self.client.get(
            "/api/v1/stats",
            headers={"X-API-Key": settings.api_key, "X-Tenant": self.tenant_a["id"]},
        )
        self.assertEqual(response.status_code, 401)
        created = self.client.post(
            "/api/v1/rfqs", json=self.PAYLOAD,
            headers={"X-API-Key": settings.api_key, "X-Tenant": self.tenant_a["id"]},
        )
        self.assertEqual(created.status_code, 401)
        self.assertEqual(self.store_a.stats()["rfqs"], 0)

    def test_owner_key_works_for_main_database(self) -> None:
        response = self.client.get("/api/v1/stats", headers={"X-API-Key": settings.api_key})
        self.assertEqual(response.status_code, 200)

    def test_client_key_without_header_is_rejected(self) -> None:
        key = registry.issue_key(self.tenant_a["id"])
        response = self.client.get("/api/v1/stats", headers={"X-API-Key": key})
        self.assertEqual(response.status_code, 401)


class SessionIsolationTest(unittest.TestCase):
    """Сессия помнит клиента: подмена куки клиента не даёт чужой доступ."""

    def setUp(self) -> None:
        self.tenant_a, self.store_a = _make_tenant("ss-a-")
        self.tenant_b, self.store_b = _make_tenant("ss-b-")
        self.store_a.create_user(email="admin-a@iso.ru", password="strongpass1", role="admin")
        self.store_b.create_user(email="admin-b@iso.ru", password="strongpass1", role="admin")
        self.client = TestClient(app, follow_redirects=False)

    def test_session_of_one_tenant_does_not_work_in_another(self) -> None:
        # администратор клиента A входит обычным путём
        login = self.client.post(
            "/ui/login", data={"email": "admin-a@iso.ru", "password": "strongpass1"}
        )
        self.assertEqual(login.status_code, 303)
        self.assertEqual(self.client.get("/ui").status_code, 200)

        # подменяем куку клиента на B (как если бы её подделали или украли)
        from kpbot.security import make_user_session

        self.client.cookies.set(
            "kpbot_tenant",
            make_user_session(settings.session_secret, self.tenant_b["id"], 86400 * 30),
        )
        page = self.client.get("/ui", follow_redirects=False)
        self.assertEqual(page.status_code, 303, "в чужой базе сессия не действует")
        self.assertIn("/ui/login", page.headers.get("location", ""))
        # и данные клиента B не показаны
        response = self.client.get("/ui")
        self.assertNotIn("admin-b@iso.ru", response.text)

    def test_client_cabinet_session_does_not_cross(self) -> None:
        from kpbot.cabinet_store import CabinetStore

        cabinet_a = CabinetStore(Path(self.tenant_a["db_path"]))
        cabinet_a.init_schema()
        company = cabinet_a.create_company(name="ООО Кабинет A", inn="7700000001")
        cabinet_a.create_user(company_id=company["id"], email="buyer@a.ru", password="buyerpass1")

        login = self.client.post(
            "/cabinet/login",
            data={"email": "buyer@a.ru", "password": "buyerpass1", "tenant": self.tenant_a["id"]},
        )
        self.assertEqual(login.status_code, 303, login.text)
        self.assertEqual(self.client.get("/cabinet").status_code, 200)

        from kpbot.security import make_user_session

        self.client.cookies.set(
            "kpbot_client_tenant",
            make_user_session(settings.session_secret, self.tenant_b["id"], 86400 * 30),
        )
        page = self.client.get("/cabinet", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertIn("/cabinet/login", page.headers.get("location", ""))


class DataSeparationTest(unittest.TestCase):
    """Данные разных клиентов физически в разных базах."""

    def test_rfqs_do_not_mix(self) -> None:
        _, store_a = _make_tenant("ds-a-")
        _, store_b = _make_tenant("ds-b-")
        cfg = Settings(data_dir=_TMP, api_key="k", ui_password="p", session_secret="s",
                       pdf_enabled=False)
        pipeline_a = Pipeline(store_a, cfg)
        pipeline_a.ensure_price_list()
        result = pipeline_a.process_text("уголок 50х50 — 5 шт", sender="a@b.ru",
                                         subject="Только для A", message_id="ds-1")
        self.assertIsNone(store_b.get_rfq(result.rfq_id))
        self.assertEqual(store_b.stats()["rfqs"], 0)
        titles = [row["subject"] for row in store_b.list_rfqs(50)]
        self.assertNotIn("Только для A", titles)


class LoginWithStaleCookieTest(unittest.TestCase):
    """Испорченная кука клиента не должна блокировать вход.

    Защита «кука есть, а базы нет» одно время перехватывала и саму страницу
    входа: человек со старой кукой не мог войти вообще — его перекидывало на
    /ui/login по кругу.
    """

    def setUp(self) -> None:
        self.store = Store(settings.db_path)
        try:
            self.store.create_user(email="stale-login@iso.ru", password="strongpass1", role="admin")
        except Exception:  # noqa: BLE001 - пользователь остался с прошлого прогона
            pass
        self.client = TestClient(app, follow_redirects=False)

    def test_login_works_with_broken_tenant_cookie(self) -> None:
        self.client.cookies.set("kpbot_tenant", "garbage.123.badsig")
        response = self.client.post(
            "/ui/login", data={"email": "stale-login@iso.ru", "password": "strongpass1"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/ui", "вход не должен зацикливаться")
        # браузер по этому ответу удаляет испорченную куку — повторяем то же
        self.assertIn("kpbot_tenant", response.headers.get("set-cookie", ""))
        self.client.cookies.delete("kpbot_tenant")
        self.assertEqual(self.client.get("/ui").status_code, 200)

    def test_login_page_clears_broken_cookie(self) -> None:
        self.client.cookies.set("kpbot_tenant", "garbage.123.badsig")
        page = self.client.get("/ui/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn("kpbot_tenant", page.headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()

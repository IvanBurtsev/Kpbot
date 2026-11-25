"""Клиент к 1С: остатки и цены по OData, создание документа.

Почему так, а не «через файл»: OData — штатный интерфейс 1С, он есть в любой
типовой конфигурации, и на клиентской базе его достаточно включить. Здесь только
сетевой слой и разбор ответов: тесты гоняют его на поддельных ответах 1С,
а на живой базе меняется лишь адрес.

Что важно про имена полей: в OData они приходят из метаданных конфигурации,
поэтому разбираем по смыслу (ключ содержит «Номенклатура», «Количество»,
«Склад»), а не по жёстким именам.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

ODATA = "/odata/standard.odata"


class OneCError(RuntimeError):
    """1С недоступна, отказала в доступе или вернула неожиданный ответ."""


@dataclass(slots=True)
class StockRow:
    sku: str
    name: str
    qty: Decimal
    unit: str = ""
    warehouse: str = ""


def _pick(record: dict, *needles: str) -> object | None:
    """Ищет значение по фрагменту имени поля: имена в разных конфигурациях разные."""
    lowered = {str(key).lower(): key for key in record}
    for needle in needles:
        for low, original in lowered.items():
            if needle in low:
                return record[original]
    return None


def _text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("Наименование", "Description", "Представление", "name"):
            if key in value:
                return str(value[key])
        return str(value.get("_Key") or value.get("Ref_Key") or "")
    return str(value)


def _number(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def parse_balance(payload: dict) -> list[tuple[str, str, str, str]]:
    """Разбирает ответ Balance регистра: (артикул, количество, единица, склад)."""
    rows = payload.get("value") or payload.get("balance") or []
    result: list[tuple[str, str, str, str]] = []
    for record in rows:
        if not isinstance(record, dict):
            continue
        name = _text(_pick(record, "номенклатура", "nomenclature", "номенклатура_"))
        sku = _text(_pick(record, "артикул", "sku", "код"))
        qty = _number(_pick(record, "количеств", "остаток баланс", "остаток", "quantity"))
        if qty is None:
            continue
        unit = _text(_pick(record, "единицаизмерения", "единица", "unit"))
        warehouse = _text(_pick(record, "склад", "warehouse"))
        key = sku or name
        if not key:
            continue
        result.append((key, str(qty), unit, warehouse))
    return result


def _exact(record: dict, *names: str) -> object | None:
    """Значение по точному имени поля (без регистра): «Номенклатура_Key» и т.п."""
    lowered = {str(key).lower(): value for key, value in record.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def _line_items(document: dict) -> list[dict]:
    """Табличная часть документа: первый список словарей с количеством."""
    for value in document.values():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(item, dict) for item in value):
            continue
        if any(_exact(item, "количество", "quantity") is not None for item in value):
            return value
    return []


# Имена документов зависят от конфигурации: пробуем по очереди, что ответит.
INCOMING_DOCUMENTS = ("Document_ПоступлениеТоваровУслуг", "Document_ПриходнаяНакладная")
OUTGOING_DOCUMENTS = ("Document_РеализацияТоваровУслуг", "Document_РасходнаяНакладная")


class OneCClient:
    """Тонкий клиент: Basic-авторизация и JSON OData."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        timeout: float = 30.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        # клиент можно подменить в тестах: сеть не нужна, ответы задаём транспортом
        self._http = http or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        url = f"{self.base_url}{ODATA}{path}"
        try:
            response = self._http.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code == 401:
            raise OneCError("1С не приняла логин или пароль (401)")
        if response.status_code == 403:
            raise OneCError("у пользователя нет прав на чтение (403)")
        if response.status_code >= 400:
            body = response.text[:200].replace("\n", " ")
            raise OneCError(f"1С ответила {response.status_code}: {body}")
        return response

    def check(self) -> str:
        """Проверка доступа: читаем метаданные OData."""
        response = self._get("/$metadata")
        if "<edmx" not in response.text.lower():
            raise OneCError("ответ не похож на метаданные OData")
        return f"доступ есть, метаданные {len(response.text)} символов"

    def stock(self, limit: int = 1000) -> list[tuple[str, str, str, str]]:
        """Остатки товаров на складах из регистра «Товары на складах»."""
        response = self._get(
            "/AccumulationRegister_ТоварыНаСкладах/Balance",
            params={"$format": "json", "$top": limit},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneCError("1С вернула не JSON (вероятно, открылась веб-страница)") from exc
        rows = parse_balance(payload)
        if not rows:
            raise OneCError("в ответе нет остатков: проверьте склад и права пользователя")
        return rows

    # ---------- остатки по документам (когда регистр не опубликован) ----------

    def _json(self, path: str, params: dict | None = None) -> dict:
        response = self._get(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise OneCError("1С вернула не JSON (вероятно, открылась веб-страница)") from exc

    def nomenclature(self, limit: int = 5000) -> dict[str, tuple[str, str, str]]:
        """Справочник номенклатуры: Ref_Key → (артикул, наименование, единица)."""
        payload = self._json("/Catalog_Номенклатура", {"$format": "json", "$top": limit})
        result: dict[str, tuple[str, str, str]] = {}
        for record in payload.get("value") or []:
            if not isinstance(record, dict):
                continue
            ref = _text(_exact(record, "ref_key", "ref"))
            if not ref:
                continue
            sku = _text(_exact(record, "артикул", "код", "code"))
            name = _text(_exact(record, "наименование", "description"))
            unit = _text(_exact(record, "единицаизмерения", "единица", "unit"))
            result[ref] = (sku or name, name, unit)
        return result

    def warehouses(self, limit: int = 1000) -> dict[str, str]:
        """Склады: Ref_Key → название. Нет справочника — пустая карта."""
        try:
            payload = self._json("/Catalog_Склады", {"$format": "json", "$top": limit})
        except OneCError:
            return {}
        result: dict[str, str] = {}
        for record in payload.get("value") or []:
            if not isinstance(record, dict):
                continue
            ref = _text(_exact(record, "ref_key", "ref"))
            if ref:
                result[ref] = _text(_exact(record, "description", "наименование"))
        return result

    def documents(self, entity: str, limit: int = 1000) -> list[dict]:
        """Документы указанного вида. Нет такого вида в конфигурации — пусто."""
        try:
            payload = self._json(f"/{entity}", {"$format": "json", "$top": limit})
        except OneCError:
            return []
        return [item for item in payload.get("value") or [] if isinstance(item, dict)]

    def stock_from_documents(self, limit: int = 1000) -> list[tuple[str, str, str, str]]:
        """Остатки как «приход − расход» по документам.

        Нужен, когда регистр остатков не опубликован, а документы поступления
        и реализации в базе есть. Артикул берём из справочника номенклатуры
        по Ref_Key: в табличной части документа лежит ссылка, а не артикул.
        """
        nomenclature = self.nomenclature()
        warehouses = self.warehouses()
        totals: dict[str, Decimal] = {}
        units: dict[str, str] = {}
        warehouse_by_sku: dict[str, str] = {}
        found = False

        for entities, sign in ((INCOMING_DOCUMENTS, 1), (OUTGOING_DOCUMENTS, -1)):
            for entity in entities:
                for document in self.documents(entity, limit):
                    items = _line_items(document)
                    if not items:
                        continue
                    found = True
                    warehouse_ref = _text(_exact(document, "склад_key", "склад"))
                    warehouse = warehouses.get(warehouse_ref, "") or warehouse_ref
                    for item in items:
                        ref = _text(_exact(item, "номенклатура_key", "номенклатура_ref"))
                        qty = _number(_exact(item, "количество", "quantity"))
                        if qty is None:
                            continue
                        sku, _, unit = nomenclature.get(ref, ("", "", ""))
                        if not sku:
                            # бывает, что табличная часть развёрнута: артикул рядом
                            sku = _text(_exact(item, "артикул", "sku")) or ref
                            unit = unit or _text(_exact(item, "единицаизмерения", "единица", "unit"))
                        if not sku:
                            continue
                        totals[sku] = totals.get(sku, Decimal(0)) + qty * sign
                        units.setdefault(sku, unit)
                        if warehouse:
                            warehouse_by_sku.setdefault(sku, warehouse)

        if not found:
            raise OneCError(
                "нет документов поступления/реализации: нечего считать. "
                "Либо опубликуйте регистр остатков, либо заведите документы в 1С"
            )

        return [
            (sku, format(qty.normalize(), "f"), units.get(sku, ""), warehouse_by_sku.get(sku, ""))
            for sku, qty in sorted(totals.items())
        ]

    def stock_auto(self, limit: int = 1000) -> list[tuple[str, str, str, str]]:
        """Сначала регистр, при отказе — документы. Ошибку отдаём только если не вышло ни то, ни другое."""
        try:
            return self.stock(limit)
        except OneCError as register_error:
            try:
                return self.stock_from_documents(limit)
            except OneCError as documents_error:
                raise OneCError(
                    f"регистр: {register_error}; документы: {documents_error}"
                ) from documents_error

    def ref_by_sku(self) -> dict[str, str]:
        """Обратная карта справочника: артикул → Ref_Key (для записи документов)."""
        result: dict[str, str] = {}
        for ref, (sku, _name, _unit) in self.nomenclature().items():
            if sku:
                result.setdefault(sku, ref)
        return result

    def create_order(
        self,
        lines: list[tuple[str, str, str]],
        *,
        counterparty_key: str = "",
        date: str | None = None,
    ) -> str:
        """Создаёт «Заказ клиента» по строкам (артикул, количество, цена).

        Пишем только то, что знаем наверняка: дату, товары и — если передан —
        контрагента. Остальные реквизиты 1С заполнит сама. Если конфигурация
        не принимает запись, вернём её текст как есть: это не наша ошибка.
        """
        if not lines:
            raise OneCError("пустой заказ: в расчёте нет строк")

        refs = self.ref_by_sku()
        missing = [sku for sku, _qty, _price in lines if sku not in refs]
        if missing:
            raise OneCError(
                "в справочнике 1С нет артикулов: " + ", ".join(missing[:5])
                + (" и ещё " + str(len(missing) - 5) if len(missing) > 5 else "")
            )

        goods = [
            {
                "Номенклатура_Key": refs[sku],
                "Количество": str(qty),
                "Цена": str(price),
            }
            for sku, qty, price in lines
        ]
        payload: dict[str, object] = {
            "Date": date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "Товары": goods,
        }
        if counterparty_key:
            payload["Контрагент_Key"] = counterparty_key

        url = f"{self.base_url}{ODATA}/Document_ЗаказКлиента?$format=json"
        try:
            response = self._http.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            raise OneCError(f"1С отказала в создании заказа ({response.status_code}): {body}")
        try:
            created = response.json()
        except ValueError:
            return "создан"
        return str(created.get("Ref_Key") or created.get("Number") or "создан")

    def create_invoice(self, *, counterparty: str, lines: list[tuple[str, str, str]]) -> str:
        """Создаёт счёт на оплату. Возвращает ссылку на документ.

        lines: (ключ номенклатуры, количество, цена).
        """
        payload = {
            "Date": None,
            "Контрагент_Key": counterparty,
            "Товары": [
                {"Номенклатура_Key": sku, "Количество": qty, "Цена": price}
                for sku, qty, price in lines
            ],
        }
        url = f"{self.base_url}{ODATA}/Document_СчетНаОплатуПокупателю?$format=json"
        try:
            response = self._http.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code >= 400:
            raise OneCError(f"1С отказала в создании документа ({response.status_code}): {response.text[:180]}")
        try:
            created = response.json()
        except ValueError:
            return "создан"
        return str(created.get("Ref_Key") or created.get("Number") or "создан")
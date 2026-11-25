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


class OneCResponseError(OneCError):
    """Ответ 1С с HTTP-кодом: нужен, чтобы отличать 404 от прочих ошибок."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


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
# Регистры остатков: в УТ это «Товары на складах», в других конфигурациях — иначе.
STOCK_REGISTERS = (
    "AccumulationRegister_ТоварыНаСкладах",
    "AccumulationRegister_ЗапасыНаСкладах",
    "AccumulationRegister_СвободныеОстатки",
)
# «Заказ клиента» есть в УТ/ERP, но не в Бухгалтерии.
CUSTOMER_ORDER_DOCUMENTS = ("Document_ЗаказКлиента", "Document_ЗаказПокупателя")


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
            raise OneCResponseError("1С не приняла логин или пароль (401)", 401)
        if response.status_code == 403:
            raise OneCResponseError("у пользователя нет прав на чтение (403)", 403)
        if response.status_code >= 400:
            body = response.text[:200].replace("\n", " ")
            raise OneCResponseError(f"1С ответила {response.status_code}: {body}", response.status_code)
        return response

    def check(self) -> str:
        """Проверка доступа: читаем метаданные OData."""
        response = self._get("/$metadata")
        if "<edmx" not in response.text.lower():
            raise OneCError("ответ не похож на метаданные OData")
        return f"доступ есть, метаданные {len(response.text)} символов"

    def stock(self, limit: int = 1000) -> list[tuple[str, str, str, str]]:
        """Остатки из регистра. Регистр зависит от конфигурации: пробуем известные."""
        tried: list[str] = []
        for register in STOCK_REGISTERS:
            try:
                payload = self._json(
                    f"/{register}/Balance", {"$format": "json", "$top": limit}
                )
            except OneCResponseError as exc:
                if exc.status == 404:  # такого регистра в конфигурации нет
                    tried.append(register)
                    continue
                raise
            rows = parse_balance(payload)
            if rows:
                return rows
            tried.append(register)
        raise OneCError(
            "регистр остатков не найден или в нём нет остатков; пробовали: "
            + ", ".join(tried)
            + ". Для Бухгалтерии остатки считаются по документам: --source documents"
        )

    # ---------- справочники ----------

    def units(self, limit: int = 1000) -> dict[str, str]:
        """Единицы измерения: Ref_Key → «шт», «кг». Нет справочника — пусто."""
        try:
            payload = self._json(
                "/Catalog_КлассификаторЕдиницИзмерения", {"$format": "json", "$top": limit}
            )
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

    def _first(self, entity: str, *, select: str = "Ref_Key") -> str:
        """Первый Ref_Key справочника: организация, склад, валюта по умолчанию."""
        try:
            payload = self._json(f"/{entity}", {"$format": "json", "$top": 1, "$select": select})
        except OneCError:
            return ""
        for record in payload.get("value") or []:
            if isinstance(record, dict):
                return _text(_exact(record, "ref_key"))
        return ""

    # ---------- остатки по документам (когда регистр не опубликован) ----------

    def _json(self, path: str, params: dict | None = None) -> dict:
        response = self._get(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise OneCError("1С вернула не JSON (вероятно, открылась веб-страница)") from exc

    def nomenclature(self, limit: int = 5000) -> dict[str, tuple[str, str, str]]:
        """Справочник номенклатуры: Ref_Key → (артикул, наименование, единица).

        Единица в справочнике хранится ссылкой, поэтому её название берём
        из классификатора единиц измерения. Папки пропускаем.
        """
        payload = self._json("/Catalog_Номенклатура", {"$format": "json", "$top": limit})
        units = self.units()
        result: dict[str, tuple[str, str, str]] = {}
        for record in payload.get("value") or []:
            if not isinstance(record, dict) or record.get("IsFolder"):
                continue
            ref = _text(_exact(record, "ref_key", "ref"))
            if not ref:
                continue
            sku = _text(_exact(record, "артикул", "код", "code"))
            name = _text(_exact(record, "наименование", "description"))
            unit_ref = _text(_exact(record, "единицаизмерения_key", "единицаизмерения"))
            unit = units.get(unit_ref) or _text(_exact(record, "единицаизмерения", "unit"))
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

    def _entity_exists(self, entity: str) -> bool:
        """Есть ли такая сущность в конфигурации (404 = нет)."""
        try:
            self._json(f"/{entity}", {"$format": "json", "$top": 1})
            return True
        except OneCResponseError as exc:
            if exc.status == 404:
                return False
            raise

    def create_order(
        self,
        lines: list[tuple[str, str, str]],
        *,
        counterparty_key: str = "",
        date: str | None = None,
    ) -> str:
        """Создаёт «Заказ клиента» по строкам (артикул, количество, цена).

        В Бухгалтерии такого документа нет — тогда честно говорим об этом.
        """
        if not lines:
            raise OneCError("пустой заказ: в расчёте нет строк")

        entity = next(
            (name for name in CUSTOMER_ORDER_DOCUMENTS if self._entity_exists(name)), ""
        )
        if not entity:
            raise OneCError(
                "в этой конфигурации нет «Заказа клиента» (похоже на Бухгалтерию). "
                "Для неё используйте счёт на оплату или файл для 1С"
            )
        return self._create_document(entity, "Товары", lines, counterparty_key, date)

    def create_invoice(
        self,
        *,
        counterparty: str,
        lines: list[tuple[str, str, str]],
        organization: str = "",
        warehouse: str = "",
        currency: str = "",
        date: str | None = None,
    ) -> str:
        """Создаёт «Счёт на оплату покупателю». Возвращает ссылку или номер.

        Реквизиты, которые можно взять из базы (организация, склад, валюта),
        подставляем из первых записей справочников: их обычно одна.
        """
        if not lines:
            raise OneCError("пустой счёт: в расчёте нет строк")
        if not counterparty:
            raise OneCError("не передан контрагент: без него 1С счёт не запишет")

        organization = organization or self._first("Catalog_Организации")
        warehouse = warehouse or self._first("Catalog_Склады")
        currency = currency or self._first("Catalog_Валюты")
        if not organization:
            raise OneCError("в базе 1С нет организации — заполните её и повторите")

        return self._create_document(
            "Document_СчетНаОплатуПокупателю",
            "Товары",
            lines,
            counterparty,
            date,
            extra={
                "Организация_Key": organization,
                "ВалютаДокумента_Key": currency,
                "СуммаДокумента": _total(lines),
                "СуммаВключаетНДС": True,
                **({"Склад_Key": warehouse} if warehouse else {}),
            },
        )

    def _create_document(
        self,
        entity: str,
        tabular: str,
        lines: list[tuple[str, str, str]],
        counterparty: str,
        date: str | None,
        extra: dict | None = None,
    ) -> str:
        refs = self.ref_by_sku()
        missing = [sku for sku, _qty, _price in lines if sku not in refs]
        if missing:
            raise OneCError(
                "в справочнике 1С нет артикулов: " + ", ".join(missing[:5])
                + (" и ещё " + str(len(missing) - 5) if len(missing) > 5 else "")
            )

        payload: dict[str, object] = {
            "Date": date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            tabular: [
                {
                    "Номенклатура_Key": refs[sku],
                    "Количество": str(qty),
                    "Цена": str(price),
                }
                for sku, qty, price in lines
            ],
        }
        if counterparty:
            payload["Контрагент_Key"] = counterparty
        payload.update(extra or {})

        url = f"{self.base_url}{ODATA}/{entity}?$format=json"
        try:
            response = self._http.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            hint = (
                " Табличную часть 1С Fresh часто не принимает через OData — "
                "тогда выгрузите файл для 1С и загрузите вручную."
                if response.status_code == 500
                else ""
            )
            raise OneCError(f"1С отказала в создании документа ({response.status_code}): {body}.{hint}")
        try:
            created = response.json()
        except ValueError:
            return "создан"
        return str(created.get("Ref_Key") or created.get("Number") or "создан")


def _total(lines: list[tuple[str, str, str]]) -> str:
    """Сумма документа по строкам (количество × цена)."""
    total = Decimal(0)
    for _sku, qty, price in lines:
        try:
            total += Decimal(str(qty)) * Decimal(str(price))
        except InvalidOperation:
            continue
    return format(total.normalize(), "f")
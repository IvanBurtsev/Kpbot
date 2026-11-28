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
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .one_c_profiles import detect_configuration, parse_entities

import httpx

ODATA = "/odata/standard.odata"

log = logging.getLogger("kpbot.one_c")


class OneCError(RuntimeError):
    """1С недоступна, отказала в доступе или вернула неожиданный ответ."""


class OneCResponseError(OneCError):
    """Ответ 1С с HTTP-кодом: нужен, чтобы отличать 404 от прочих ошибок."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _error_hint(text: str) -> str:
    """Подсказка к частым ошибкам 1С: сам текст платформы ничего не объясняет.

    «Перечисление 'СтавкиНДС' не содержит элемент 'НДС22'» значит, что в базе
    устаревшая конфигурация: основной ставки 22% (ст. 164 НК РФ, с 01.01.2026)
    в перечислении нет. Понижать ставку в kpbot нельзя — документ станет
    неверным, а НДС недоплаченным; обновляется конфигурация 1С.
    """
    low = (text or "").lower()
    if "не содержит элемент" in low and "ндс" in low:
        return (
            "\nв базе 1С нет ставки НДС 22% — это устаревшая конфигурация:"
            " с 01.01.2026 основная ставка в РФ 22% (ст. 164 НК РФ),"
            " обновите конфигурацию 1С. Ставку в kpbot понижать нельзя:"
            " счёт станет неверным"
        )
    if "передзаписью" in low:
        return (
            "\n1С отбила запись в «ПередЗаписью»: документ нужно создавать"
            " в интерфейсе или дать пользователю API полные права"
        )
    return ""


def _error_text(response: httpx.Response) -> str:
    """Достаёт человеческий текст из OData-ошибки 1С, иначе — начало тела."""
    try:
        data = response.json()
        message = (
            (data.get("odata.error") or {}).get("message") or {}
        ).get("value") or data.get("error") or data.get("message")
    except ValueError:
        message = None
    text = str(message or response.text or "без описания").replace("\n", " ")
    return text[:400]


@dataclass(slots=True)
class StockRow:
    sku: str
    name: str
    qty: Decimal
    unit: str = ""
    warehouse: str = ""


def _pick(record: dict, *needles: str) -> object | None:
    """Ищет значение по фрагменту имени поля: имена в разных конфигурациях разные.

    Представление важнее ключа: в ответе 1С рядом лежат «Номенклатура» (текст)
    и «Номенклатура_Key» (GUID). Раньше бралось первое попавшееся поле, и
    остаток мог оказаться привязан к GUID — тогда склад/артикул не находились.
    """
    fields = [(str(key).lower(), key) for key in record]
    for needle in needles:
        for low, original in fields:
            if low == needle and not low.endswith("_key"):
                return record[original]
    for needle in needles:
        for low, original in fields:
            if needle in low and not low.endswith("_key"):
                return record[original]
    for needle in needles:
        for low, original in fields:
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
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = str(value).strip()
    for space in ("\u00a0", "\u202f", "\u2009", "\u2007", " "):
        text = text.replace(space, "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


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


_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def merge_stock_rows(
    rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Складывает остатки одного артикула по складам.

    Регистр «Товары на складах» всегда многомерен по складу: одна номенклатура
    на двух складах — это две строки ответа. В таблице остатков артикул —
    первичный ключ, поэтому такие строки роняли импорт (UNIQUE constraint),
    а без импорта «остатки из 1С» не работали вовсе. Для вопроса «сколько есть»
    правильный ответ — сумма по складам.
    """
    totals: dict[str, Decimal] = {}
    units: dict[str, str] = {}
    warehouses: dict[str, list[str]] = {}
    for key, qty_text, unit, warehouse in rows:
        qty = _number(qty_text)
        if qty is None:
            continue
        totals[key] = totals.get(key, Decimal("0")) + qty
        if unit:
            units.setdefault(key, unit)
        if warehouse:
            warehouses.setdefault(key, []).append(warehouse)
    return [
        (
            key,
            format(qty.normalize(), "f"),
            units.get(key, ""),
            ", ".join(dict.fromkeys(warehouses.get(key, [])))[:120],
        )
        for key, qty in totals.items()
    ]


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


_NEXT_LINK_KEYS = ("odata.nextLink", "@odata.nextLink", "nextLink")


def _next_link(payload: dict) -> str:
    for key in _NEXT_LINK_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class OneCClient:
    """Тонкий клиент: Basic-авторизация и JSON OData."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        timeout: float = 30.0,
        http: httpx.Client | None = None,
        service_url: str = "",
        service_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.service_url = (service_url or "").strip()
        self.service_token = (service_token or "").strip()
        self._entities: set[str] | None = None
        # клиент можно подменить в тестах: сеть не нужна, ответы задаём транспортом
        self._http = http or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> httpx.Response:
        """Один сетевой вызов с понятными ошибками и текстом от 1С."""
        url = f"{self.base_url}{ODATA}{path}"
        return self._request_url(method, url, params=params, body=body)

    def _request_url(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method, url, headers=self._headers(), params=params, json=body
            )
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code == 401:
            raise OneCResponseError("1С не приняла логин или пароль (401)", 401)
        if response.status_code == 403:
            raise OneCResponseError("у пользователя нет прав на это действие (403)", 403)
        if response.status_code >= 400:
            raise OneCResponseError(
                f"1С ответила {response.status_code}: {_error_text(response)}"
                + _error_hint(_error_text(response)),
                response.status_code,
            )
        return response

    def create_entity(self, entity: str, payload: dict) -> dict:
        response = self._request("POST", f"/{entity}", params={"$format": "json"}, body=payload)
        try:
            return response.json()
        except ValueError:
            return {}

    def update_entity(self, entity: str, ref: str, payload: dict) -> dict:
        response = self._request(
            "PATCH",
            f"/{entity}(guid'{ref}')",
            params={"$format": "json"},
            body=payload,
        )
        try:
            return response.json()
        except ValueError:
            return {}

    def delete_entity(self, entity: str, ref: str) -> None:
        self._request("DELETE", f"/{entity}(guid'{ref}')", params={"$format": "json"})

    def post_document(self, entity: str, ref: str) -> None:
        """Проведение документа: OData-метод Post()."""
        self._request(
            "POST", f"/{entity}(guid'{ref}')/Post()", params={"$format": "json"}, body={}
        )

    def unpost_document(self, entity: str, ref: str) -> None:
        """Отмена проведения: OData-метод Unpost()."""
        self._request(
            "POST", f"/{entity}(guid'{ref}')/Unpost()", params={"$format": "json"}, body={}
        )

    def get_entity(self, entity: str, ref: str, *, select: str = "") -> dict:
        params = {"$format": "json"}
        if select:
            params["$select"] = select
        return self._request("GET", f"/{entity}(guid'{ref}')", params=params).json()

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
                records = self._json_all(
                    f"/{register}/Balance", {"$format": "json", "$top": limit}
                )
            except OneCResponseError as exc:
                if exc.status == 404:  # такого регистра в конфигурации нет
                    tried.append(register)
                    continue
                raise
            rows = parse_balance({"value": records})
            if rows:
                # Склады складываются, GUID-ы превращаются в артикулы: иначе
                # импорт остатков падал на дублях, а остаток не находился по SKU.
                return merge_stock_rows(self._resolve_stock_skus(rows))
            tried.append(register)
        raise OneCError(
            "регистр остатков не найден или в нём нет остатков; пробовали: "
            + ", ".join(tried)
            + ". Для Бухгалтерии остатки считаются по документам: --source documents"
        )

    def _resolve_stock_skus(
        self, rows: list[tuple[str, str, str, str]]
    ) -> list[tuple[str, str, str, str]]:
        """GUID номенклатуры → артикул: в регистре часто нет поля «Артикул».

        Ключ остатка обязан совпадать с артикулом прайса, иначе `stock_map()`
        не найдёт позицию и кабинет покажет «нет на складе» по всему каталогу.
        """
        if not any(_GUID.match(key) for key, *_ in rows):
            return rows
        try:
            names = self.nomenclature()
        except OneCError:
            log.warning("не удалось прочитать номенклатуру: остатки останутся по GUID")
            return rows
        resolved: list[tuple[str, str, str, str]] = []
        for key, qty, unit, warehouse in rows:
            sku, _name, unit_from_catalog = names.get(key, ("", "", ""))
            resolved.append((sku or key, qty, unit or unit_from_catalog, warehouse))
        return resolved

    # ---------- справочники ----------

    def units(self, limit: int = 1000) -> dict[str, str]:
        """Единицы измерения: Ref_Key → «шт», «кг». Нет справочника — пусто."""
        try:
            records = self._json_all(
                "/Catalog_КлассификаторЕдиницИзмерения", {"$format": "json", "$top": limit}
            )
        except OneCError:
            return {}
        result: dict[str, str] = {}
        for record in records:
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

    def _json_absolute(self, url: str) -> dict:
        """Читает абсолютный адрес (odata.nextLink) с теми же ошибками, что и _json."""
        response = self._request_url("GET", url)
        try:
            return response.json()
        except ValueError as exc:
            raise OneCError("1С вернула не JSON на странице nextLink") from exc

    def _json_all(
        self, path: str, params: dict | None = None, *, max_pages: int = 20
    ) -> list[dict]:
        """Все записи, а не первая страница.

        1С ограничивает размер страницы и отдаёт следующую ссылку в
        `odata.nextLink`. Раньше ссылка игнорировалась: на большом справочнике
        или регистре остатков часть позиций молча исчезала, и заявка получала
        «нет в наличии» по ходовым артикулам.
        """
        payload = self._json(path, params=params)
        items = [item for item in payload.get("value") or [] if isinstance(item, dict)]
        link = _next_link(payload)
        pages = 1
        while link and pages < max_pages:
            payload = self._json_absolute(link)
            items.extend(item for item in payload.get("value") or [] if isinstance(item, dict))
            link = _next_link(payload)
            pages += 1
        if link:
            log.warning("1С вернула больше %s страниц — остаток не дочитан", max_pages)
        return items

    def nomenclature(self, limit: int = 5000) -> dict[str, tuple[str, str, str]]:
        """Справочник номенклатуры: Ref_Key → (артикул, наименование, единица).

        Единица в справочнике хранится ссылкой, поэтому её название берём
        из классификатора единиц измерения. Папки пропускаем.
        """
        records = self._json_all("/Catalog_Номенклатура", {"$format": "json", "$top": limit})
        units = self.units()
        result: dict[str, tuple[str, str, str]] = {}
        for record in records:
            if not isinstance(record, dict) or record.get("IsFolder") or record.get("DeletionMark"):
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
            records = self._json_all("/Catalog_Склады", {"$format": "json", "$top": limit})
        except OneCError:
            return {}
        result: dict[str, str] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            ref = _text(_exact(record, "ref_key", "ref"))
            if ref:
                result[ref] = _text(_exact(record, "description", "наименование"))
        return result

    def documents(self, entity: str, limit: int = 1000) -> list[dict]:
        """Документы указанного вида. Нет такого вида в конфигурации — пусто."""
        try:
            return self._json_all(f"/{entity}", {"$format": "json", "$top": limit})
        except OneCError:
            return []

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

    def prices(self, limit: int = 5000) -> list[dict]:
        """Цены номенклатуры из регистра сведений: [{sku, name, unit, price}].

        Берём последнюю запись по каждой номенклатуре: регистр периодический.
        """
        rows = self._json_all(
            "/InformationRegister_ЦеныНоменклатуры", {"$format": "json", "$top": limit}
        )
        latest: dict[str, tuple[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ref = _text(_exact(row, "номенклатура_key", "номенклатура"))
            price = _number(_exact(row, "цена", "price"))
            if not ref or price is None:
                continue
            period = _text(_exact(row, "period", "период", "date"))
            current = latest.get(ref)
            if current is None or period >= current[0]:
                latest[ref] = (period, format(price.normalize(), "f"))
        if not latest:
            raise OneCError(
                "в 1С нет цен номенклатуры: заполните регистр «Цены номенклатуры» "
                "или загрузите прайс файлом"
            )

        names = self.nomenclature()
        result: list[dict] = []
        for ref, (_period, price) in latest.items():
            sku, name, unit = names.get(ref, (ref, "", ""))
            result.append({"sku": sku, "name": name, "unit": unit, "price": price})
        return sorted(result, key=lambda item: item["sku"])


    def entity_sets(self, refresh: bool = False) -> set[str]:
        """Все сущности OData: по ним определяется конфигурация и её возможности."""
        if self._entities is None or refresh:
            self._entities = parse_entities(self._get("/$metadata").text)
        return self._entities

    def diagnose(self) -> dict:
        """Что умеет база: конфигурация, доступные чтения, признаки записи."""
        report = detect_configuration(self.entity_sets())
        report["entities_total"] = len(self._entities or ())
        reads: dict[str, dict] = {}
        probes = {
            "nomenclature": lambda: f"{len(self.nomenclature())} позиций",
            "prices": lambda: f"{len(self.prices())} цен",
            "stock": lambda: f"{len(self.stock())} строк остатков",
            "warehouses": lambda: f"{len(self.warehouses())} складов",
        }
        for name, probe in probes.items():
            try:
                reads[name] = {"ok": True, "detail": probe()}
            except OneCError as exc:
                reads[name] = {"ok": False, "detail": str(exc)[:200]}
        report["reads"] = reads
        report["write_via_service"] = bool(self.service_url)
        return report

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
        post: bool = False,
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
        return self._create_document(
            entity, "Товары", lines, counterparty_key, date, post=post
        )

    def create_invoice(
        self,
        *,
        counterparty: str,
        lines: list[tuple[str, str, str]],
        organization: str = "",
        warehouse: str = "",
        currency: str = "",
        date: str | None = None,
        vat_rate: Decimal = Decimal("0.22"),
        line_vats: list[Decimal] | None = None,
        prices_include_vat: bool = False,
        post: bool = False,
    ) -> str:
        """Создаёт «Счёт на оплату покупателю» и возвращает Ref_Key.

        Схема проверена на живой Бухгалтерии 3.0:
        1) POST шапки (без табличной части) → 201 и Ref_Key;
        2) PATCH табличной части полным массивом, с LineNumber, Суммой и НДС;
        3) при необходимости Post() — проведение.
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
                "СуммаВключаетНДС": False,
                "ДокументБезНДС": vat_rate <= 0,
                **({"Склад_Key": warehouse} if warehouse else {}),
            },
            vat_rate=Decimal(str(vat_rate)),
            line_vats=line_vats,
            prices_include_vat=prices_include_vat,
            post=post,
        )

    def create_invoice_via_service(
        self,
        *,
        counterparty: str,
        lines: list[tuple[str, str, str]],
        organization: str = "",
        warehouse: str = "",
        date: str | None = None,
        comment: str = "",
        vat_rate: Decimal | None = None,
        line_vats: list[Decimal] | None = None,
        prices_include_vat: bool = False,
    ) -> str:
        """Создаёт счёт через HTTP-сервис 1С (`deploy/1c/kpbot-invoice.bsl`).

        Это обход ограничения OData: табличную часть документа пишет сама 1С,
        поэтому работает и в Бухгалтерии на Fresh. Адрес сервиса —
        KPBOT_ONE_C_SERVICE_URL, например https://host/base/hs/kpbot/invoice.
        """
        if not self.service_url:
            raise OneCError(
                "не задан KPBOT_ONE_C_SERVICE_URL — адрес HTTP-сервиса 1С "
                "(см. docs/ONE_C_HTTP_SERVICE.md)"
            )
        if not lines:
            raise OneCError("пустой счёт: в расчёте нет строк")
        if not counterparty:
            raise OneCError("не передан контрагент: без него 1С счёт не запишет")

        payload_lines = []
        cent = Decimal("0.01")
        for index, (sku, qty, price) in enumerate(lines):
            quantity = Decimal(str(qty))
            unit_price = Decimal(str(price))
            gross_or_net = (quantity * unit_price).quantize(cent, rounding=ROUND_HALF_UP)
            supplied = None
            if line_vats is not None and index < len(line_vats):
                supplied = Decimal(str(line_vats[index])).quantize(cent, rounding=ROUND_HALF_UP)
            if supplied is not None:
                vat = supplied
                net = gross_or_net - vat if prices_include_vat else gross_or_net
            elif vat_rate is not None and prices_include_vat and vat_rate > 0:
                net = (gross_or_net / (Decimal("1") + vat_rate)).quantize(
                    cent, rounding=ROUND_HALF_UP
                )
                vat = gross_or_net - net
            else:
                net = gross_or_net
                vat = (
                    (net * vat_rate).quantize(cent, rounding=ROUND_HALF_UP)
                    if vat_rate is not None and vat_rate > 0
                    else Decimal("0.00")
                )
            net_price = (net / quantity).quantize(cent, rounding=ROUND_HALF_UP) if quantity else unit_price
            payload_lines.append(
                {
                    "sku": sku,
                    "qty": str(quantity),
                    "price": str(net_price),
                    "net": str(net),
                    "vat_amount": str(vat),
                    "vat": int((Decimal(str(vat_rate)) * 100).to_integral_value())
                    if vat_rate is not None
                    else 0,
                }
            )

        payload = {
            "counterparty": counterparty,
            "organization": organization,
            "warehouse": warehouse,
            "date": date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "comment": comment,
            "prices_include_vat": bool(prices_include_vat),
            "lines": payload_lines,
        }
        headers = dict(self._headers())
        if self.service_token:
            headers["X-Kpbot-Token"] = self.service_token
        try:
            response = self._http.post(
                self.service_url, headers=headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise OneCError(f"сеть: {type(exc).__name__}: {str(exc)[:120]}") from exc
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            raise OneCError(f"HTTP-сервис 1С ответил {response.status_code}: {body}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OneCError("HTTP-сервис вернул не JSON — проверьте публикацию сервиса") from exc
        if not data.get("ok"):
            raise OneCError(f"1С не создала счёт: {data.get('error') or 'без описания'}")
        return str(data.get("number") or data.get("ref") or "создан")

    def _create_document(
        self,
        entity: str,
        tabular: str,
        lines: list[tuple[str, str, str]],
        counterparty: str,
        date: str | None,
        extra: dict | None = None,
        *,
        vat_rate: Decimal | None = None,
        line_vats: list[Decimal] | None = None,
        prices_include_vat: bool = False,
        post: bool = False,
    ) -> str:
        """Шапка → табличная часть → (по желанию) проведение.

        Так работает OData в Бухгалтерии 3.0: вложенную табличную часть в POST
        платформа не принимает, а PATCH с полным массивом строк — принимает.
        """
        refs = self.ref_by_sku()
        missing = [sku for sku, _qty, _price in lines if sku not in refs]
        if missing:
            raise OneCError(
                "в справочнике 1С нет артикулов: " + ", ".join(missing[:5])
                + (" и ещё " + str(len(missing) - 5) if len(missing) > 5 else "")
            )

        header: dict[str, object] = {
            "Date": date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if counterparty:
            header["Контрагент_Key"] = counterparty
        header.update(extra or {})

        created = self.create_entity(entity, header)
        ref = str(created.get("Ref_Key") or "")
        if not ref:
            raise OneCError("1С создала документ, но не вернула Ref_Key — проверьте права и поля")

        rows: list[dict[str, object]] = []
        total = Decimal(0)
        cent = Decimal("0.01")
        for number, (sku, qty, price) in enumerate(lines, start=1):
            quantity = Decimal(str(qty))
            unit_price = Decimal(str(price))
            gross_or_net = (quantity * unit_price).quantize(cent, rounding=ROUND_HALF_UP)
            row: dict[str, object] = {
                "LineNumber": str(number),
                "Номенклатура_Key": refs[sku],
                "Количество": float(quantity),
                "Цена": float(unit_price),
            }
            if vat_rate is None:
                net = gross_or_net
                row["Сумма"] = float(net)
            else:
                supplied = None
                if line_vats is not None and number - 1 < len(line_vats):
                    supplied = Decimal(str(line_vats[number - 1])).quantize(
                        cent, rounding=ROUND_HALF_UP
                    )
                if supplied is not None:
                    vat = supplied
                    net = gross_or_net - vat if prices_include_vat else gross_or_net
                elif prices_include_vat and vat_rate > 0:
                    net = (gross_or_net / (Decimal("1") + vat_rate)).quantize(
                        cent, rounding=ROUND_HALF_UP
                    )
                    vat = gross_or_net - net
                else:
                    net = gross_or_net
                    vat = (net * vat_rate).quantize(cent, rounding=ROUND_HALF_UP) if vat_rate > 0 else Decimal("0.00")
                row["Сумма"] = float(net)
                row["СуммаНДС"] = float(vat)
                row["СтавкаНДС"] = _vat_enum(vat_rate)
                total += net + vat
                rows.append(row)
                continue
            total += net
            rows.append(row)

        try:
            self.update_entity(
                entity,
                ref,
                {tabular: rows, "СуммаДокумента": float(total)},
            )
            if post:
                self.post_document(entity, ref)
        except Exception:
            # Шапка уже создана: без отката в базе остаётся пустой документ,
            # который никто не найдёт (1С отбила ставку НДС или табличную часть).
            try:
                self.delete_entity(entity, ref)
            except Exception as cleanup_error:  # noqa: BLE001 - главная ошибка важнее
                log.warning("не удалось удалить пустой документ 1С %s: %s", ref, cleanup_error)
            raise
        return ref


def client_from_settings(cfg) -> OneCClient:
    """Клиент по настройкам приложения: один адрес для CLI и веб-интерфейса."""
    if not cfg.one_c_base_url:
        raise OneCError("не задан KPBOT_ONE_C_BASE_URL — адрес базы 1С")
    return OneCClient(
        cfg.one_c_base_url,
        cfg.one_c_user,
        cfg.one_c_password,
        cfg.one_c_timeout_seconds,
        service_url=cfg.one_c_service_url,
        service_token=cfg.one_c_service_token,
    )


def _vat_enum(rate: Decimal) -> str:
    """Ставка НДС для поля «СтавкаНДС»: имена перечислений в 1С."""
    rate = Decimal(str(rate))
    if rate <= 0:
        return "БезНДС"
    percent = int((rate * 100).to_integral_value())
    # 20/18 — только для чтения старых документов (до 2026). Новые счета с
    # этими ставками не выпускаются: с 01.01.2026 основная ставка — 22%.
    return {
        20: "НДС20",
        18: "НДС18",
        10: "НДС10",
        7: "НДС7",
        5: "НДС5",
        22: "НДС22",
    }.get(percent, f"НДС{percent}")


def _total(lines: list[tuple[str, str, str]]) -> str:
    """Сумма документа по строкам (количество × цена)."""
    total = Decimal(0)
    for _sku, qty, price in lines:
        try:
            total += Decimal(str(qty)) * Decimal(str(price))
        except InvalidOperation:
            continue
    return format(total.normalize(), "f")

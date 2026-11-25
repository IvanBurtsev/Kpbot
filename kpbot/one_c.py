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
        """Остатки товаров на складах."""
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
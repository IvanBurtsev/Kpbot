"""Профили конфигураций 1С: определить, что база умеет, и что включить.

Метаданные OData содержат имена сущностей; по их набору можно отличить
УТ/КА/ERP от Бухгалтерии и понять, какие возможности доступны: остатки,
заказы, счета, цены. Это основа мастера подключения (1c-setup).
"""

from __future__ import annotations

import re

# Признаки возможностей: ищем сущности по имени, а не по жёсткому списку.
FEATURE_ENTITIES = {
    "nomenclature": ("Catalog_Номенклатура",),
    "units": ("Catalog_КлассификаторЕдиницИзмерения",),
    "warehouses": ("Catalog_Склады",),
    "counterparties": ("Catalog_Контрагенты",),
    "organizations": ("Catalog_Организации",),
    "prices": ("InformationRegister_ЦеныНоменклатуры",),
    "stock_register": (
        "AccumulationRegister_ТоварыНаСкладах",
        "AccumulationRegister_ЗапасыНаСкладах",
        "AccumulationRegister_СвободныеОстатки",
    ),
    "orders": ("Document_ЗаказКлиента", "Document_ЗаказПокупателя"),
    "invoice": ("Document_СчетНаОплатуПокупателю",),
    "sales": ("Document_РеализацияТоваровУслуг",),
    "purchase": ("Document_ПоступлениеТоваровУслуг",),
}

FEATURE_LABELS = {
    "nomenclature": "Номенклатура",
    "units": "Единицы измерения",
    "warehouses": "Склады",
    "counterparties": "Контрагенты",
    "organizations": "Организации",
    "prices": "Цены номенклатуры",
    "stock_register": "Регистр остатков",
    "orders": "Заказы клиента",
    "invoice": "Счета на оплату",
    "sales": "Реализация",
    "purchase": "Поступление",
}

ENTITY_RE = re.compile(r'EntitySet Name="([^"]+)"')


def parse_entities(metadata_xml: str) -> set[str]:
    """Имена сущностей из $metadata."""
    return set(ENTITY_RE.findall(metadata_xml or ""))


def detect_features(entities: set[str]) -> dict[str, bool]:
    return {
        name: any(entity in entities for entity in candidates)
        for name, candidates in FEATURE_ENTITIES.items()
    }


def detect_configuration(entities: set[str]) -> dict:
    """Определяет конфигурацию и список доступных возможностей."""
    features = detect_features(entities)
    if features["orders"] and features["stock_register"]:
        configuration = "УТ 11 / КА 2 / ERP 2 (заказы и остатки)"
        note = "полный набор: заказы, остатки, счета; запись — через HTTP-сервис"
    elif features["orders"]:
        configuration = "УТ-подобная (заказы есть, регистра остатков нет)"
        note = "остатки не найдены: проверьте публикацию регистра или склад"
    elif features["invoice"]:
        configuration = "Бухгалтерия 3.0"
        note = "чтение справочников и счёт; заказов клиента в конфигурации нет"
    else:
        configuration = "неизвестная конфигурация"
        note = "не нашли ни заказов, ни счетов — проверьте права и состав метаданных"

    return {
        "configuration": configuration,
        "note": note,
        "features": features,
        "supported_write": bool(features["invoice"] or features["orders"]),
    }

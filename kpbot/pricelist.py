"""Прайс-лист: генерация демо-номенклатуры и загрузка из CSV/XLSX."""

from __future__ import annotations

import csv
import sys
from decimal import Decimal
from pathlib import Path

from .domain import PriceItem, ValidationError

# Локальные библиотеки (в песочнице нет доступа к PyPI).
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# (sku, наименование, единица, цена без НДС)
DEFAULT_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("KR-001", "Уголок монтажный 50х50 мм стальной", "шт", "34.50"),
    ("KR-002", "Уголок монтажный 40х40 мм стальной", "шт", "27.00"),
    ("KR-003", "Саморез по дереву 4.2х75 мм", "уп", "310.00"),
    ("KR-004", "Саморез по металлу 3.5х25 мм", "уп", "185.00"),
    ("KR-005", "Дюбель-гвоздь 6х40 мм", "уп", "210.00"),
    ("KR-006", "Анкер клиновой 10х100 мм", "шт", "78.00"),
    ("KR-007", "Шпилька резьбовая М8", "м", "96.00"),
    ("KR-008", "Гайка М8 оцинкованная", "шт", "4.20"),
    ("KR-009", "Шайба М8 оцинкованная", "шт", "1.90"),
    ("KR-010", "Перфолента монтажная 20х1 мм", "м", "41.00"),
    ("EL-001", "Кабель ВВГнг-LS 3х2.5", "м", "128.00"),
    ("EL-002", "Кабель ВВГнг-LS 3х1.5", "м", "84.00"),
    ("EL-003", "Кабель ВВГнг-LS 5х6", "м", "402.00"),
    ("EL-004", "Провод ПуГВ 1х4", "м", "52.00"),
    ("EL-005", "Гофра ПВХ 20 мм с зондом", "м", "38.00"),
    ("EL-006", "Автомат ВА47-29 16А 1P", "шт", "340.00"),
    ("EL-007", "Автомат ВА47-29 25А 1P", "шт", "365.00"),
    ("EL-008", "УЗО 40А 30мА 2P", "шт", "1980.00"),
    ("EL-009", "Розетка 16А белая", "шт", "315.00"),
    ("EL-010", "Выключатель одноклавишный белый", "шт", "285.00"),
    ("EL-011", "Щит распределительный навесной 12 модулей", "шт", "1450.00"),
    ("EL-012", "Лампа LED 10 Вт 4000К E27", "шт", "148.00"),
    ("EL-013", "Светильник накладной 36 Вт 1200 мм", "шт", "1180.00"),
    ("SN-001", "Труба ППР 25 мм PN20", "м", "96.00"),
    ("SN-002", "Труба ППР 32 мм PN20", "м", "138.00"),
    ("SN-003", "Уголок ППР 90 градусов 25 мм", "шт", "32.00"),
    ("SN-004", "Тройник ППР 25 мм", "шт", "48.00"),
    ("SN-005", "Муфта ППР 25 мм", "шт", "28.00"),
    ("SN-006", "Кран шаровой 1/2 дюйма латунь", "шт", "620.00"),
    ("SN-007", "Смеситель для мойки", "шт", "2450.00"),
    ("SN-008", "Унитаз-компакт с бачком", "шт", "5900.00"),
    ("SN-009", "Гибкая подводка 1/2 дюйма 60 см", "шт", "190.00"),
    ("SN-010", "Сгон 1/2 дюйма стальной", "шт", "145.00"),
    ("IN-001", "Диск отрезной по металлу 125х1.2 мм", "шт", "89.00"),
    ("IN-002", "Диск отрезной по металлу 230х2 мм", "шт", "168.00"),
    ("IN-003", "Сверло по металлу 6 мм", "шт", "74.00"),
    ("IN-004", "Сверло по бетону 8х110 мм", "шт", "122.00"),
    ("IN-005", "Бур SDS-plus 10х210 мм", "шт", "285.00"),
    ("IN-006", "Коронка биметаллическая 68 мм", "шт", "890.00"),
    ("IN-007", "Изолента ПВХ 19 мм х 20 м", "шт", "46.00"),
    ("IN-008", "Стяжка нейлоновая 200х4.8 мм", "уп", "128.00"),
    ("IN-009", "Скотч малярный 48 мм х 50 м", "шт", "84.00"),
    ("IN-010", "Перчатки нитриловые размер L", "уп", "390.00"),
    ("IN-011", "Пленка укрывная 4х5 м 30 мкм", "шт", "165.00"),
    ("IN-012", "Мешки для мусора 120 л", "уп", "210.00"),
    ("MT-001", "Песок строительный навалом", "т", "890.00"),
    ("MT-002", "Цемент М500 50 кг", "меш", "520.00"),
    ("MT-003", "Гипсокартон 12.5 мм 1200х2500", "лист", "640.00"),
    ("MT-004", "Профиль ПН-27/28 3 м", "шт", "215.00"),
    ("MT-005", "Профиль ПП-60/27 3 м", "шт", "268.00"),
    ("MT-006", "Штукатурка гипсовая 30 кг", "меш", "465.00"),
    ("MT-007", "Шпаклевка финишная 25 кг", "меш", "690.00"),
    ("MT-008", "Грунтовка глубокого проникновения 10 л", "канистра", "580.00"),
    ("MT-009", "Пена монтажная 750 мл", "шт", "420.00"),
    ("MT-010", "Герметик силиконовый 310 мл", "шт", "315.00"),
)

COLUMNS = ("SKU", "Наименование", "Ед.", "Цена", "Валюта")


def default_price_items() -> list[PriceItem]:
    return [
        PriceItem(sku=sku, name=name, unit=unit, price=Decimal(price))
        for sku, name, unit, price in DEFAULT_ITEMS
    ]


def write_csv(path: Path, items: list[PriceItem] | None = None) -> Path:
    items = items or default_price_items()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(COLUMNS)
        for item in items:
            writer.writerow([item.sku, item.name, item.unit, str(item.price), item.currency])
    return path


def write_xlsx(path: Path, items: list[PriceItem] | None = None) -> Path:
    items = items or default_price_items()
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:  # pragma: no cover - зависит от окружения
        raise ValidationError("openpyxl недоступен: используйте CSV") from None

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Прайс"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for item in items:
        ws.append([item.sku, item.name, item.unit, float(item.price), item.currency])

    widths = {"A": 12, "B": 46, "C": 12, "D": 12, "E": 10}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A2"
    wb.save(path)
    return path


def load_price_items(path: Path, *, max_rows: int = 100_000) -> list[PriceItem]:
    """Читает прайс в CSV (разделитель ; или ,) или XLSX. Строки без SKU пропускаются."""
    if not path.exists():
        raise ValidationError(f"прайс-лист не найден: {path}")

    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        rows = _read_xlsx(path, max_rows=max_rows)
    else:
        rows = _read_csv(path, max_rows=max_rows)

    items: list[PriceItem] = []
    for row in rows:
        if len(row) < 4:
            continue
        sku = str(row[0] or "").strip()
        name = str(row[1] or "").strip()
        unit = str(row[2] or "").strip()
        raw_price = str(row[3] or "").replace(",", ".").replace(" ", "")
        if not sku or not name or not unit:
            continue
        try:
            price = Decimal(raw_price)
        except Exception:
            continue
        if price < 0:
            continue
        items.append(PriceItem(sku=sku, name=name, unit=unit, price=price))

    if not items:
        raise ValidationError("прайс-лист пуст или не распознан")
    return items


def _read_csv(path: Path, *, max_rows: int) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        reader = csv.reader(fh, delimiter=delimiter, strict=True)
        rows = [row for _, row in zip(range(max_rows + 1), reader)]
    return rows[1:] if rows else []


def _read_xlsx(path: Path, *, max_rows: int) -> list[list[object]]:
    from openpyxl import load_workbook

    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows: list[list[object]] = []
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            if index == 0:
                continue
            if index > max_rows:
                break
            rows.append(list(row))
        return rows
    finally:
        wb.close()
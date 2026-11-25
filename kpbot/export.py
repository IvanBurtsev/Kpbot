"""Экспорт заявки в Excel: то, что снабженец понесёт дальше в 1С или заказчику."""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from .domain import LineStatus, PriceItem

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

STATUS_LABELS = {
    LineStatus.OK.value: "в прайсе",
    LineStatus.AMBIGUOUS.value: "похоже, нужно подтверждение",
    LineStatus.NO_MATCH.value: "не найдено в прайсе",
    LineStatus.INVALID.value: "нет количества или единицы",
}


def build_rfq_xlsx(
    rfq: dict,
    quote: dict | None,
    stock: dict[str, dict] | None = None,
) -> bytes:
    """Собирает книгу: лист «Расчёт», лист «Для 1С» и строки письма.

    stock — остатки по артикулу (из store.stock_map()): нужны, чтобы менеджер
    видел склад и нехватку ещё до загрузки в 1С.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="EFEFEF")
    money_format = "#,##0.00"

    sheet = wb.active
    sheet.title = "Расчёт"

    sheet["A1"] = "Заявка"
    sheet["A1"].font = Font(bold=True, size=14)
    meta = (
        ("Номер", rfq["id"][:8].upper()),
        ("От кого", rfq["sender"]),
        ("Тема", rfq["subject"]),
        ("Получено", rfq.get("created_at", "")),
        ("Статус", rfq.get("status", "")),
    )
    for offset, (label, value) in enumerate(meta, start=3):
        sheet.cell(row=offset, column=1, value=label).font = bold
        sheet.cell(row=offset, column=2, value=str(value or ""))

    table_row = 9
    headers = ("№", "Артикул", "Наименование", "Ед.", "Кол-во", "Цена, ₽", "Сумма, ₽")
    for column, title in enumerate(headers, start=1):
        cell = sheet.cell(row=table_row, column=column, value=title)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    row = table_row + 1
    lines = (quote or {}).get("lines", [])
    for number, line in enumerate(lines, start=1):
        sheet.cell(row=row, column=1, value=number)
        sheet.cell(row=row, column=2, value=line["sku"])
        sheet.cell(row=row, column=3, value=line["name"])
        sheet.cell(row=row, column=4, value=line["unit"])
        sheet.cell(row=row, column=5, value=float(Decimal(str(line["qty"]))))
        sheet.cell(row=row, column=6, value=float(Decimal(str(line["price"]))))
        sheet.cell(row=row, column=7, value=float(Decimal(str(line["amount"]))))
        sheet.cell(row=row, column=6).number_format = money_format
        sheet.cell(row=row, column=7).number_format = money_format
        row += 1

    if quote:
        for label, key in (
            ("Сумма без НДС", "subtotal"),
            ("НДС", "vat"),
            ("Доставка", "delivery"),
            ("Итого", "total"),
        ):
            sheet.cell(row=row, column=6, value=label).font = bold
            cell = sheet.cell(row=row, column=7, value=float(Decimal(str(quote.get(key, "0")))))
            cell.number_format = money_format
            cell.font = bold if key == "total" else Font()
            row += 1

    widths = {"A": 6, "B": 14, "C": 52, "D": 8, "E": 10, "F": 12, "G": 14}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = f"A{table_row + 1}"

    # лист под справочник «Номенклатура»: так 1С создаст/обновит карточки товара
    nom = wb.create_sheet("Номенклатура")
    nom.append(("Наименование", "Артикул", "Единица измерения"))
    seen: set[str] = set()
    for line in lines:
        if line["sku"] in seen:
            continue
        seen.add(line["sku"])
        nom.append((line["name"], line["sku"], line["unit"]))
    for column, width in {"A": 60, "B": 16, "C": 18}.items():
        nom.column_dimensions[column].width = width

    # лист для загрузки в 1С: только нужные колонки, без оформления.
    # Склад и ставка НДС — дополнительные: если обработка клиента их не знает,
    # достаточно удалить две последние колонки.
    one_c = wb.create_sheet("Для 1С")
    one_c.append(("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС"))
    vat_percent = _vat_percent(quote)
    for line in lines:
        entry = (stock or {}).get(line["sku"]) or {}
        one_c.append(
            (
                line["sku"],
                line["name"],
                float(Decimal(str(line["qty"]))),
                float(Decimal(str(line["price"]))),
                float(Decimal(str(line["amount"]))),
                entry.get("warehouse") or "",
                vat_percent,
            )
        )
    for column, width in {"A": 14, "B": 52, "C": 12, "D": 12, "E": 14, "F": 18, "G": 12}.items():
        one_c.column_dimensions[column].width = width

    second = wb.create_sheet("Строки из письма")
    second_headers = ("№", "Как в письме", "Кол-во", "Ед.", "Статус", "Score", "Подобранная позиция")
    for column, title in enumerate(second_headers, start=1):
        cell = second.cell(row=1, column=column, value=title)
        cell.font = bold
        cell.fill = header_fill
    for offset, line in enumerate(rfq.get("lines", []), start=2):
        second.cell(row=offset, column=1, value=line["idx"])
        second.cell(row=offset, column=2, value=line["raw_text"])
        second.cell(row=offset, column=3, value=line.get("qty") or "")
        second.cell(row=offset, column=4, value=line.get("unit") or "")
        second.cell(row=offset, column=5, value=STATUS_LABELS.get(line["status"], line["status"]))
        second.cell(row=offset, column=6, value=round(float(line.get("score") or 0), 2))
        second.cell(row=offset, column=7, value=line.get("matched_name") or "")
    for column, width in {"A": 6, "B": 46, "C": 10, "D": 8, "E": 30, "F": 8, "G": 46}.items():
        second.column_dimensions[column].width = width

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _vat_percent(quote: dict | None) -> str:
    """0.20 → «20%». Пустая строка, если ставка неизвестна."""
    if not quote:
        return ""
    rate = quote.get("vat_rate")
    if rate in (None, ""):
        return ""
    try:
        percent = Decimal(str(rate)) * 100
    except InvalidOperation:
        return ""
    return f"{format(percent.normalize(), 'f')}%"


def check_1c_sheet(content: bytes) -> list[str]:
    """Проверяет лист «Для 1С»: колонки, типы, отсутствие пустых строк.

    Возвращает список проблем (пусто — всё в порядке). Нужно, чтобы отдавать
    клиенту файл, который точно загрузится стандартной обработкой 1С.
    """
    from openpyxl import load_workbook

    problems: list[str] = []
    wb = load_workbook(filename=BytesIO(content), read_only=True, data_only=True)
    try:
        if "Для 1С" not in wb.sheetnames:
            return ["нет листа «Для 1С»"]
        if "Номенклатура" not in wb.sheetnames:
            problems.append("нет листа «Номенклатура»")
        sheet = wb["Для 1С"]
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return ["лист «Для 1С» пуст"]

        expected = ("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС")
        header = tuple(rows[0])
        # старый пятиколоночный файл тоже годится: склад и НДС — необязательные
        if header not in (expected, expected[:5]):
            problems.append(f"шапка не совпадает: {header} вместо {expected}")

        data_rows = [row for row in rows[1:] if any(cell not in (None, "") for cell in row)]
        if not data_rows:
            problems.append("нет строк с данными")

        seen_codes: set[str] = set()
        for number, row in enumerate(data_rows, start=2):
            code, name, qty, price, amount = (row + (None,) * 5)[:5]
            if not code:
                problems.append(f"строка {number}: пустой код")
            elif str(code) in seen_codes:
                problems.append(f"строка {number}: повтор кода {code} — 1С сопоставит неоднозначно")
            else:
                seen_codes.add(str(code))
            if not name:
                problems.append(f"строка {number}: пустое наименование")
            for label, value in (("количество", qty), ("цена", price), ("сумма", amount)):
                if not isinstance(value, (int, float)):
                    problems.append(f"строка {number}: {label} не число ({value!r})")
        return problems
    finally:
        wb.close()


def build_price_xlsx(items: list[PriceItem]) -> bytes:  # pragma: no cover - вспомогательное
    from openpyxl import Workbook

    wb = Workbook()
    sheet = wb.active
    sheet.title = "Прайс"
    sheet.append(("SKU", "Наименование", "Ед.", "Цена"))
    for item in items:
        sheet.append((item.sku, item.name, item.unit, float(item.price)))
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

def build_rfq_csv(
    rfq: dict,
    quote: dict | None,
    out_dir: Path,
    stock: dict[str, dict] | None = None,
) -> list[Path]:
    """Два CSV для 1С: справочник и табличная часть документа.

    Разделитель «;», десятичная запятая, UTF-8 с BOM — так 1С и Excel
    на русской локали читают файл без танцев.
    """
    import csv

    short = str(rfq.get("id", ""))[:8]
    lines = (quote or {}).get("lines", [])
    written: list[Path] = []

    nom_path = out_dir / f"1c-nomenclature-{short}.csv"
    with nom_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(("Наименование", "Артикул", "Единица измерения"))
        seen: set[str] = set()
        for line in lines:
            if line["sku"] in seen:
                continue
            seen.add(line["sku"])
            writer.writerow((line["name"], line["sku"], line["unit"]))
    written.append(nom_path)

    order_path = out_dir / f"1c-order-{short}.csv"
    vat_percent = _vat_percent(quote)
    with order_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС"))
        for line in lines:
            entry = (stock or {}).get(line["sku"]) or {}
            writer.writerow(
                (
                    line["sku"],
                    line["name"],
                    _ru_number(line["qty"]),
                    _ru_number(line["price"]),
                    _ru_number(line["amount"]),
                    entry.get("warehouse") or "",
                    vat_percent,
                )
            )
    written.append(order_path)
    return written


def _ru_number(value: object) -> str:
    """1234.5 → «1234,50»: 1С и Excel на русской локали ждут запятую."""
    from decimal import Decimal

    amount = Decimal(str(value))
    return f"{amount:.2f}".replace(".", ",")

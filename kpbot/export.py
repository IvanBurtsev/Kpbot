"""Экспорт заявки в Excel: то, что снабженец понесёт дальше в 1С или заказчику."""

from __future__ import annotations

import sys
from decimal import Decimal
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


def build_rfq_xlsx(rfq: dict, quote: dict | None) -> bytes:
    """Собирает книгу: лист «Расчёт» и лист «Строки из письма»."""
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

    # лист для загрузки в 1С: только нужные колонки, без оформления
    one_c = wb.create_sheet("Для 1С")
    one_c.append(("Код", "Наименование", "Количество", "Цена", "Сумма"))
    for line in lines:
        one_c.append(
            (
                line["sku"],
                line["name"],
                float(Decimal(str(line["qty"]))),
                float(Decimal(str(line["price"]))),
                float(Decimal(str(line["amount"]))),
            )
        )
    for column, width in {"A": 14, "B": 52, "C": 12, "D": 12, "E": 14}.items():
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

        expected = ("Код", "Наименование", "Количество", "Цена", "Сумма")
        if tuple(rows[0]) != expected:
            problems.append(f"шапка не совпадает: {tuple(rows[0])} вместо {expected}")

        data_rows = [row for row in rows[1:] if any(cell not in (None, "") for cell in row)]
        if not data_rows:
            problems.append("нет строк с данными")

        for number, row in enumerate(data_rows, start=2):
            code, name, qty, price, amount = (row + (None,) * 5)[:5]
            if not code:
                problems.append(f"строка {number}: пустой код")
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
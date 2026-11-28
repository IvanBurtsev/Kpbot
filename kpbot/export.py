"""Экспорт заявки в Excel: то, что снабженец понесёт дальше в 1С или заказчику."""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from .domain import LineStatus, PriceItem

CENT = Decimal("0.01")


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _quote_rate(quote: dict | None) -> Decimal:
    try:
        return Decimal(str((quote or {}).get("vat_rate") or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _quote_inclusive(quote: dict | None) -> bool:
    return str((quote or {}).get("prices_include_vat") or "0").strip().lower() in {
        "1", "true", "yes", "on", "да"
    }


def _line_split(line: dict, rate: Decimal, inclusive: bool) -> tuple[Decimal, Decimal, Decimal]:
    """(без НДС, НДС, с НДС) для строки снимка КП.

    Снимки нового формата хранят net/vat; старые (и тесты) — только amount,
    тогда считаем по ставке. В Excel нельзя показывать сумму с НДС под
    заголовком «Сумма без НДС» — это и была ошибка в режиме «цены с НДС».
    """
    amount = _money(line.get("amount"))
    has_split = "net" in line and "vat" in line
    if has_split:
        net = _money(line.get("net"))
        vat = _money(line.get("vat"))
        if not (net == 0 and vat == 0 and amount != 0):
            return net, vat, _money(net + vat)
    if inclusive and rate > 0:
        net = (amount / (Decimal("1") + rate)).quantize(CENT, rounding=ROUND_HALF_UP)
        return net, _money(amount - net), amount
    vat = (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP) if rate > 0 else Decimal("0.00")
    return amount, vat, _money(amount + vat)

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    # В конец: установленные пакеты важнее вендорной копии, vendor — запасной путь.
    sys.path.append(str(_VENDOR))

STATUS_LABELS = {
    LineStatus.OK.value: "в прайсе",
    LineStatus.AMBIGUOUS.value: "похоже, нужно подтверждение",
    LineStatus.NO_MATCH.value: "не найдено в прайсе",
    LineStatus.INVALID.value: "нет количества или единицы",
}

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: object) -> str:
    """Строка для ячейки, которая не станет формулой.

    openpyxl (и Excel при открытии CSV) считает формулой всё, что начинается
    с «= + - @». Текст письма пишет посторонний человек, поэтому такие строки
    помечаем апострофом — Excel покажет их как текст, а не выполнит.
    """
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


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
        sheet.cell(row=offset, column=2, value=safe_text(value))

    table_row = 9
    headers = ("№", "Артикул", "Наименование", "Ед.", "Кол-во", "Цена, ₽", "Сумма, ₽")
    for column, title in enumerate(headers, start=1):
        cell = sheet.cell(row=table_row, column=column, value=title)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    row = table_row + 1
    lines = (quote or {}).get("lines", [])
    rate = _quote_rate(quote)
    inclusive = _quote_inclusive(quote)
    for number, line in enumerate(lines, start=1):
        net, _vat, _gross = _line_split(line, rate, inclusive)
        qty = Decimal(str(line.get("qty") or "0"))
        net_price = (net / qty).quantize(CENT, rounding=ROUND_HALF_UP) if qty else _money(line.get("price"))
        sheet.cell(row=row, column=1, value=number)
        sheet.cell(row=row, column=2, value=safe_text(line["sku"]))
        sheet.cell(row=row, column=3, value=safe_text(line["name"]))
        sheet.cell(row=row, column=4, value=safe_text(line["unit"]))
        sheet.cell(row=row, column=5, value=float(qty))
        sheet.cell(row=row, column=6, value=float(net_price))
        sheet.cell(row=row, column=7, value=float(net))
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
        nom.append((safe_text(line["name"]), safe_text(line["sku"]), safe_text(line["unit"])))
    if _money((quote or {}).get("delivery")):
        nom.append(("Доставка", "DELIVERY", ""))
    for column, width in {"A": 60, "B": 16, "C": 18}.items():
        nom.column_dimensions[column].width = width

    # лист для загрузки в 1С: только нужные колонки, без оформления.
    # Склад и ставка НДС — дополнительные: если обработка клиента их не знает,
    # достаточно удалить две последние колонки.
    one_c = wb.create_sheet("Для 1С")
    one_c.append(("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС"))
    for line in lines:
        entry = (stock or {}).get(line["sku"]) or {}
        net, _vat, _gross = _line_split(line, rate, inclusive)
        qty = Decimal(str(line.get("qty") or "0"))
        net_price = (net / qty).quantize(CENT, rounding=ROUND_HALF_UP) if qty else _money(line.get("price"))
        one_c.append(
            (
                safe_text(line["sku"]),
                safe_text(line["name"]),
                float(qty),
                float(net_price),
                float(net),
                entry.get("warehouse") or "",
                _line_vat_percent(line, _vat_percent(quote)),
            )
        )
    delivery = _money((quote or {}).get("delivery"))
    if delivery:
        delivery_vat = _money((quote or {}).get("delivery_vat"))
        delivery_net = delivery - delivery_vat if inclusive else delivery
        one_c.append(("DELIVERY", "Доставка", 1, float(delivery_net), float(delivery_net), "", _vat_percent(quote)))
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
        second.cell(row=offset, column=2, value=safe_text(line["raw_text"]))
        second.cell(row=offset, column=3, value=line.get("qty") or "")
        second.cell(row=offset, column=4, value=safe_text(line.get("unit") or ""))
        second.cell(row=offset, column=5, value=STATUS_LABELS.get(line["status"], line["status"]))
        second.cell(row=offset, column=6, value=round(float(line.get("score") or 0), 2))
        second.cell(row=offset, column=7, value=safe_text(line.get("matched_name") or ""))
    for column, width in {"A": 6, "B": 46, "C": 10, "D": 8, "E": 30, "F": 8, "G": 46}.items():
        second.column_dimensions[column].width = width

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _vat_percent(quote: dict | None) -> str:
    """0.22 → «22%». Пустая строка, если ставка неизвестна."""
    if not quote:
        return ""
    rate = quote.get("vat_rate")
    if rate in (None, ""):
        return ""
    return _rate_percent(rate)


def _rate_percent(rate: object) -> str:
    try:
        percent = Decimal(str(rate)) * 100
    except (InvalidOperation, ValueError):
        return ""
    return f"{format(percent.normalize(), 'f')}%"


def _line_vat_percent(line: dict, default_percent: str) -> str:
    """Ставка конкретной строки: из снимка, иначе ставка клиента."""
    raw = line.get("vat_rate")
    if raw in (None, ""):
        return default_percent
    return _rate_percent(raw)


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
            writer.writerow((safe_text(line["name"]), safe_text(line["sku"]), safe_text(line["unit"])))
    written.append(nom_path)

    order_path = out_dir / f"1c-order-{short}.csv"
    rate = _quote_rate(quote)
    inclusive = _quote_inclusive(quote)
    with order_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС"))
        for line in lines:
            entry = (stock or {}).get(line["sku"]) or {}
            net, _vat, _gross = _line_split(line, rate, inclusive)
            qty = Decimal(str(line.get("qty") or "0"))
            net_price = (net / qty).quantize(CENT, rounding=ROUND_HALF_UP) if qty else _money(line.get("price"))
            writer.writerow(
                (
                    safe_text(line["sku"]),
                    safe_text(line["name"]),
                    _ru_number(qty),
                    _ru_number(net_price),
                    _ru_number(net),
                    entry.get("warehouse") or "",
                    _line_vat_percent(line, _vat_percent(quote)),
                )
            )
        delivery = _money((quote or {}).get("delivery"))
        if delivery:
            delivery_vat = _money((quote or {}).get("delivery_vat"))
            delivery_net = delivery - delivery_vat if inclusive else delivery
            writer.writerow(("DELIVERY", "Доставка", "1", _ru_number(delivery_net),
                             _ru_number(delivery_net), "", _vat_percent(quote)))
    written.append(order_path)
    return written


def _ru_number(value: object) -> str:
    """1234.5 → «1234,50»: 1С и Excel на русской локали ждут запятую."""
    from decimal import Decimal

    amount = Decimal(str(value))
    return f"{amount:.2f}".replace(".", ",")

"""Прайс-лист: генерация демо-номенклатуры и загрузка из CSV/XLSX."""

from __future__ import annotations

import csv
import io
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import LEGAL_VAT_RATES
from .domain import PriceItem, ValidationError
from .extract import canonical_unit

log = logging.getLogger("kpbot.pricelist")

_THOUSANDS_SPACES = ("\u00a0", "\u202f", "\u2009", "\u2007", " ")


def vat_rate_from_text(raw: object) -> Decimal | None:
    """Ставка НДС из ячейки прайса: «22», «22%», «0.22», «без НДС».

    Пусто — ставка клиента по умолчанию (None). «Без НДС» — это 0, а не
    «не задано»: для необлагаемых товаров документ должен это показать.
    """
    text = str(raw or "").strip().lower().replace("ё", "е")
    if not text:
        return None
    text = text.replace("%", "").replace("ндс", "").replace("ставка", "").strip()
    if text in {"без", "нет", "не облагается", "-"}:
        return Decimal("0")
    text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError(f"ставка НДС не число: {raw!r}") from exc
    if value > 1:
        value = value / 100
    if not (0 <= value <= 1):
        raise ValidationError(f"ставка НДС вне 0..100%: {raw!r}")
    return value


def decimal_from_text(raw: object) -> Decimal:
    """Число из ячейки прайса: «1 234,56», «1.234,56», «1,234.56», «1 234,56 ₽».

    Раньше строка с неразрывным пробелом между разрядами («1 234,50» — так
    сохраняет Excel и выгружает 1С) молча выпадала из прайса: позиция исчезала,
    и заявка по ней уходила в «не найдено». Пробелы-разделители разрядов,
    знак рубля и оба варианта десятичного разделителя понимаются явно.

    Ошибка — ValidationError: вызывающий код решает, пропустить строку или
    отказать файлу целиком, но не «молча потерять цену».
    """
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, bool) or raw is None:
        raise ValidationError("пустое число")
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        text = str(raw).strip()
        for space in _THOUSANDS_SPACES:
            text = text.replace(space, "")
        for marker in ("₽", "руб.", "руб", "р."):
            text = text.replace(marker, "")
        text = text.strip()
        if not text:
            raise ValidationError("пустое число")
        if "," in text and "." in text:
            # последний разделитель — десятичный, другой — разделитель разрядов
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ValidationError(f"не число: {raw!r}") from exc
    if not value.is_finite():
        raise ValidationError(f"не число: {raw!r}")
    return value

# Локальные библиотеки (в песочнице нет доступа к PyPI).
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    # В конец: установленные пакеты важнее вендорной копии, vendor — запасной путь.
    sys.path.append(str(_VENDOR))

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


_VAT_INCLUDED_MARKERS = (
    "сндс",
    "сналогом",
    "сучетомндс",
    "сучетомналога",
    "втчндс",
    "включаяндс",
    "gross",
    "incl",
)
_VAT_EXCLUDED_MARKERS = ("безндс", "безналога", "безналога", "net", "excl")


def _header_text(name: object) -> str:
    """Заголовок колонки в сравнимом виде: «Цена с учётом НДС» → «ценасучетомндс»."""
    return str(name or "").strip().lower().replace("ё", "е").replace(".", "").replace(" ", "")


def _header_means_inclusive(cleaned: str) -> bool:
    """«Цена с НДС», «цена с учётом НДС», «цена, в т.ч. НДС» — цена уже с налогом.

    Раньше признаком были только литералы «сндс»/«сналогом», поэтому обычные
    формулировки «с учётом НДС» и «в т.ч. НДС» принимались за цену без НДС,
    и налог начислялся второй раз — КП дороже реальности ровно на ставку.
    """
    if any(marker in cleaned for marker in _VAT_EXCLUDED_MARKERS):
        return False
    return any(marker in cleaned for marker in _VAT_INCLUDED_MARKERS)


def _pick_price_column(names: list[str]) -> int | None:
    """Колонка с ценой: «Цена без НДС» приоритетнее «Цена с НДС».

    В прайсах часто стоят обе колонки, и раньше бралась первая по порядку —
    обычно «с НДС», после чего НДС начислялся на неё второй раз: документ
    дороже реальности ровно на ставку.
    """
    candidates: list[tuple[int, str]] = []
    for index, name in enumerate(names):
        cleaned = _header_text(name)
        if cleaned.startswith(("цена", "price", "стоимость")):
            candidates.append((index, cleaned))
    if not candidates:
        return None
    for index, cleaned in candidates:
        if any(marker in cleaned for marker in _VAT_EXCLUDED_MARKERS):
            return index
    for index, cleaned in candidates:
        if not _header_means_inclusive(cleaned):
            return index
    # только колонки «с НДС»: цену без НДС взять неоткуда
    return candidates[0][0]


def _header_indexes(header: list[object]) -> dict[str, int] | None:
    """Распознаёт шапку выгрузки из 1С/Excel: колонки могут называться по-разному.

    Возвращает индексы колонок по смыслу или None, если шапка не похожа на прайс.
    """
    names = [str(cell or "").strip().lower().replace("ё", "е") for cell in header]

    def find(aliases: tuple[str, ...]) -> int | None:
        for index, name in enumerate(names):
            cleaned = _header_text(name)
            for alias in aliases:
                if cleaned == alias or cleaned.startswith(alias):
                    return index
        return None

    sku = find(("sku", "артикул", "код", "code", "номенклатуракод"))
    name = find(("наименование", "номенклатура", "name", "товар"))
    unit = find(("единица", "ед", "unit", "едизм"))
    price = _pick_price_column(names)
    currency = find(("валюта", "currency", "вал"))
    vat = find(("ставкандс", "ставка", "ндс", "vat"))
    # «с НДС» в заголовке цены — отдельный признак, а не часть имени колонки
    price_header = _header_text(names[price]) if price is not None else ""
    price_includes_vat = bool(price_header) and _header_means_inclusive(price_header)

    if name is None or price is None:
        return None
    return {
        "sku": sku if sku is not None else -1,
        "name": name,
        "unit": unit if unit is not None else -1,
        "price": price,
        "currency": currency if currency is not None else -1,
        "vat_rate": vat if vat is not None else -1,
        "price_includes_vat": 1 if price_includes_vat else 0,
    }


def normalize_unit_label(raw: str) -> str:
    """Единица прайса в той же форме, что и в письме: «кв.м» → «м2».

    Иначе строка «100 м2» и позиция с единицей «кв.м» не совпадут, и заявка
    уйдёт человеку на проверку без причины.
    """
    return canonical_unit(raw) or "шт"


def merge_prices(
    existing: list[PriceItem], incoming: list[dict]
) -> tuple[list[PriceItem], int]:
    """Добавляет/обновляет цены из 1С, сохраняя названия и единицы.

    incoming: [{sku, name, unit, price}]. Позиции, которых нет в 1С, не удаляем:
    клиент мог держать в прайсе то, чего в базе ещё нет.
    Возвращает (новый список, сколько изменилось).
    """
    by_sku = {item.sku: item for item in existing}
    changed = 0
    for row in incoming:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        try:
            price = Decimal(str(row.get("price")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if price < 0:
            continue
        current = by_sku.get(sku)
        if current is None:
            by_sku[sku] = PriceItem(
                sku=sku,
                name=str(row.get("name") or sku),
                unit=normalize_unit_label(str(row.get("unit") or "шт")),
                price=price,
            )
            changed += 1
        elif current.price != price:
            by_sku[sku] = PriceItem(
                sku=sku,
                name=current.name or str(row.get("name") or sku),
                unit=current.unit or normalize_unit_label(str(row.get("unit") or "шт")),
                price=price,
                currency=current.currency,
                vat_rate=current.vat_rate,
            )
            changed += 1
    return sorted(by_sku.values(), key=lambda item: item.sku), changed


_HEADER_HINTS = (
    "наименование", "номенклатура", "артикул", "sku", "цена", "price",
    "стоимость", "единица", "едизм", "количество", "колво", "товар", "код",
)


def _looks_like_header(row: list[object] | None) -> bool:
    """Похожа ли первая строка на шапку (а не на данные).

    Нужна, чтобы отличить «файл без шапки» (тогда позиционный разбор — это
    единственный вариант) от «шапка есть, но мы её не поняли» (тогда разбор
    по номерам колонок перепутает поля и молча испортит прайс).
    """
    if not row:
        return False
    text = "".join(_header_text(cell) for cell in row)
    hits = sum(1 for hint in _HEADER_HINTS if hint in text)
    return hits >= 2


def load_price_items(
    path: Path, *, max_rows: int = 100_000, prices_include_vat: bool = False
) -> list[PriceItem]:
    """Читает прайс из CSV (; или ,) или XLSX.

    Понимает как наш шаблон (SKU/Наименование/Ед./Цена), так и выгрузку из 1С
    (Код/Номенклатура/Единица/Цена) — колонки ищутся по смыслу, а не по номеру.

    Два молчаливых искажения суммы закрыты здесь же:

    * колонка «Цена с НДС» при выключенном режиме «цены с НДС» — отказ с
      объяснением, а не двойной НДС в документе;
    * прайс в чужой валюте — отказ: рубли из долларов никто не пересчитывает.
    """
    if not path.exists():
        raise ValidationError(f"прайс-лист не найден: {path}")

    suffix = path.suffix.lower()
    header, rows = (
        _read_xlsx_with_header(path, max_rows=max_rows)
        if suffix == ".xlsx"
        else _read_csv_with_header(path, max_rows=max_rows)
    )

    columns = _header_indexes(header) if header else None
    if columns is None:
        if _looks_like_header(header):
            # Шапка есть, но не распознана: позиционный разбор перепутал бы
            # артикул с наименованием, а цену — с единицей, и импорт «успешно»
            # испортил бы весь прайс. Лучше отказать с понятным текстом.
            raise ValidationError(
                "шапка прайса не распознана: нужны колонки с наименованием и ценой "
                "(подойдут «Артикул/Код», «Наименование/Номенклатура», «Ед.», «Цена»)"
            )
        # шапки нет вовсе (голая выгрузка) — читаем позиционно, как раньше.
        # Первую строку reader отдал как «шапку», но это данные — возвращаем её.
        if header and any(str(cell or "").strip() for cell in header):
            rows = [header, *rows]
        columns = {"sku": 0, "name": 1, "unit": 2, "price": 3, "currency": -1}
    if columns.get("price_includes_vat") and not prices_include_vat:
        raise ValidationError(
            "в прайсе колонка «Цена с НДС», а расчёт считает цены без НДС: "
            "НДС начислился бы второй раз. Включите KPBOT_PRICES_INCLUDE_VAT=1 "
            "или выгрузите прайс с ценами без НДС"
        )

    items: list[PriceItem] = []
    by_sku: dict[str, PriceItem] = {}
    duplicates = 0
    skipped_prices = 0
    foreign: set[str] = set()
    for row in rows:
        if len(row) <= max(columns["name"], columns["price"]):
            continue

        def cell(key: str) -> str:
            index = columns.get(key, -1)
            if index < 0 or index >= len(row):
                return ""
            return str(row[index] or "").strip()

        sku = cell("sku")
        name = cell("name")
        unit = normalize_unit_label(cell("unit") or "шт")
        currency = _currency_code(cell("currency"))
        if currency != "RUB":
            foreign.add(currency)
            continue
        if not name:
            continue
        if not sku:
            # Артикул не задан: генерируем свободный, а не занятый в этом файле.
            auto_index = len(items) + 1
            sku = f"AUTO-{auto_index:05d}"
            while sku in by_sku:
                auto_index += 1
                sku = f"AUTO-{auto_index:05d}"
        try:
            price = decimal_from_text(cell("price"))
        except ValidationError:
            # Битая цена — не повод молча выкинуть позицию: считаем и говорим.
            skipped_prices += 1
            continue
        if price < 0:
            skipped_prices += 1
            continue
        try:
            line_vat = vat_rate_from_text(cell("vat_rate"))
        except ValidationError as exc:
            raise ValidationError(f"строка «{name}»: {exc}") from None
        if line_vat is not None and line_vat not in LEGAL_VAT_RATES:
            raise ValidationError(
                f"строка «{name}»: ставка НДС {line_vat * 100:g}% не применяется "
                "с 01.01.2026; законные: 22%, 10%, 7%, 5%, 0%"
            )
        item = PriceItem(
            sku=sku, name=name, unit=unit, price=price, currency=currency, vat_rate=line_vat
        )
        if sku in by_sku:
            # Дубль артикула — обычная ошибка выгрузки. Раньше он ронял загрузку
            # всего прайса (UNIQUE constraint), теперь побеждает последняя строка.
            duplicates += 1
        by_sku[sku] = item
        items.append(item)

    if duplicates:
        log.warning("в прайсе %s строк с повторяющимся артикулом — оставлена последняя", duplicates)
    if skipped_prices:
        log.warning("в прайсе %s строк с нечитаемой или отрицательной ценой — пропущены", skipped_prices)
    if foreign:
        raise ValidationError(
            f"прайс в валюте {', '.join(sorted(foreign))}: цены не пересчитываются "
            "автоматически, выгрузите прайс в рублях"
        )
    if not items:
        raise ValidationError("прайс-лист пуст или не распознан")
    # Порядок строк прайса не важен, но стабильный порядок упрощает тесты и diff.
    return list(by_sku.values())


_RUBLE_MARKS = {"", "rub", "rur", "руб", "руб.", "р", "₽", "643"}


def _currency_code(raw: str) -> str:
    """Валюта строки прайса: пусто и «руб.» — это рубли."""
    text = (raw or "").strip().lower()
    if text in _RUBLE_MARKS:
        return "RUB"
    if text in {"usd", "$", "долл", "доллар", "840"}:
        return "USD"
    if text in {"eur", "€", "евро", "978"}:
        return "EUR"
    return text.upper()[:8] or "RUB"


def _read_text_file(path: Path) -> str:
    """Текст выгрузки: UTF-8 с BOM, UTF-16 с BOM или windows-1251.

    «Сохранить как CSV» в русской Windows даёт cp1251, а 1С умеет выгружать
    UTF-16 — раньше такой файл падал с UnicodeDecodeError вместо импорта.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _read_csv_with_header(path: Path, *, max_rows: int) -> tuple[list[str], list[list[str]]]:
    text = _read_text_file(path)
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    # strict=False: неровные кавычки в выгрузке 1С/Excel — не повод отказать файлу
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    all_rows = [row for _, row in zip(range(max_rows + 1), reader)]
    if not all_rows:
        return [], []
    return list(all_rows[0]), all_rows[1:]


def _read_xlsx_with_header(path: Path, *, max_rows: int) -> tuple[list[object], list[list[object]]]:
    from openpyxl import load_workbook

    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        ws = wb.active
        header: list[object] = []
        rows: list[list[object]] = []
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            if index == 0:
                header = list(row)
                continue
            if index > max_rows:
                break
            rows.append(list(row))
        return header, rows
    finally:
        wb.close()

def load_stock_items(path: Path) -> list[tuple[str, str, str, str]]:
    """Остатки из выгрузки 1С: артикул, количество, единица, склад.

    Понимает отчёты «Остатки товаров» и «Ведомость по товарам на складах»:
    колонки ищутся по смыслу, лишние (цена, сумма) игнорируются.
    """
    if not path.exists():
        raise ValidationError(f"файл остатков не найден: {path}")

    suffix = path.suffix.lower()
    header, rows = (
        _read_xlsx_with_header(path, max_rows=100_000)
        if suffix == ".xlsx"
        else _read_csv_with_header(path, max_rows=100_000)
    )
    names = [str(cell or "").strip().lower().replace("ё", "е") for cell in header] if header else []

    def find(*aliases: str) -> int | None:
        for index, name in enumerate(names):
            cleaned = _header_text(name)
            for alias in aliases:
                if cleaned == alias or cleaned.startswith(alias):
                    return index
        return None

    sku_index = find("артикул", "код", "sku", "номенклатуракод")
    qty_index = find("остаток", "количество", "колво", "qty")
    unit_index = find("единица", "ед", "unit")
    warehouse_index = find("склад", "warehouse")

    if qty_index is None:
        raise ValidationError("в файле не найдена колонка с остатком или количеством")

    # Один артикул может стоять в нескольких складах: в таблице остатков
    # sku — первичный ключ, поэтому дубли роняли импорт (UNIQUE constraint).
    # Для «сколько всего есть» количества складываются.
    totals: dict[str, Decimal] = {}
    units: dict[str, str] = {}
    warehouses: dict[str, list[str]] = {}
    for row in rows:
        if qty_index >= len(row):
            continue
        try:
            qty = decimal_from_text(row[qty_index])
        except ValidationError:
            continue
        sku = ""
        if sku_index is not None and sku_index < len(row):
            sku = str(row[sku_index] or "").strip()
        name = next(
            (str(cell or "").strip() for i, cell in enumerate(row)
             if i not in (sku_index, qty_index, unit_index, warehouse_index) and str(cell or "").strip()),
            "",
        )
        key = sku or name
        if not key:
            continue
        unit = str(row[unit_index] or "").strip() if unit_index is not None and unit_index < len(row) else ""
        warehouse = str(row[warehouse_index] or "").strip() if warehouse_index is not None and warehouse_index < len(row) else ""
        totals[key] = totals.get(key, Decimal("0")) + qty
        units.setdefault(key, unit)
        if warehouse:
            warehouses.setdefault(key, []).append(warehouse)

    if not totals:
        raise ValidationError("остатки не распознаны: нет строк с количеством")
    return [
        (
            key,
            format(qty.normalize(), "f"),
            units.get(key, ""),
            ", ".join(dict.fromkeys(warehouses.get(key, [])))[:120],
        )
        for key, qty in totals.items()
    ]

"""Извлечение позиций из текста письма.

Правиловый экстрактор: детерминированный, без сети, без галлюцинаций.
Интерфейс LineExtractor позволяет подставить LLM-адаптер (structured output +
валидация той же pydantic-схемой), не меняя остальной пайплайн.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .domain import RfqLine, quantity

MAX_LINES = 200

_SEGMENT_SPLIT = re.compile(r"[\n;]+")
_SENTENCE_SPLIT = re.compile(r"\.\s+(?=[А-ЯЁA-Z])")
# «и» разделяет позиции: «труба 20 и муфта 25». Запятые разбираются отдельно:
# «уголок 50х50, 200 шт» — это одна позиция, а не две (см. _split_commas).
_SUB_SPLIT = re.compile(r"\s+и\s+(?=[А-Яа-яЁёA-Za-z\d])")
_COMMA = re.compile(r",\s*")
# Название позиции обязано содержать слово: «1 шт» — это не товар, а обрывок.
_HAS_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{3,}")
_BULLET = re.compile(r"^\s*(?:\d{1,3}[.)]\s*|[-\u2013\u2014\u2022*\u00b7]\s*)+")
_GREETING = re.compile(
    r"^(?:добрый день|добрый вечер|доброе утро|здравствуйте|привет|коллеги)[!,.\s]*",
    re.IGNORECASE,
)
_NOISE = re.compile(
    r"^(с уважением|спасибо|итого|всего|доставка|адрес|тел|телефон|почта|e-?mail|контакт|"
    r"здравствуйте|добрый день|добрый вечер|доброе утро|срок|оплата)\b",
    re.IGNORECASE,
)
_LEAD_WORDS = re.compile(
    r"^(нужно|надо|требуется|прошу|просим|необходимо|интересует|интересуют|пришлите|посчитайте)\s+",
    re.IGNORECASE,
)
_TRAIL = re.compile(r"[\s.,:;\-–—]+$")

# «10 000», «1 500», «1 234 567» — в деловой переписке тысячи разделяют пробелом
# (обычным, неразрывным или тонким). Склеиваем их ДО разбора, иначе «1 500 м»
# читается как «1» + «500 м», и заявка тихо уменьшается в тридцать раз.
def _glue_thousands(text: str) -> str:
    """Склеивает «10 000» → «10000», но не трогает размеры и количества подряд.

    «Уголок 50х50 200 шт» — это размер 50х50 и количество 200, а не 50200.
    «кабель 3х2.5 150 м» — это размер 3х2.5 и количество 150, а не 5150:
    поэтому группа цифр склеивается, только если она начинается на границе
    слова, а точка и запятая границей не считаются (после них идёт дробная
    часть размера, а не новое число).
    """
    out: list[str] = []
    index = 0
    text = text or ""
    while index < len(text):
        char = text[index]
        if not char.isdigit():
            out.append(char)
            index += 1
            continue
        end = index
        while end < len(text) and text[end].isdigit():
            end += 1
        run = text[index:end]
        first_len = len(run)
        previous = text[index - 1] if index else ""
        boundary_ok = not previous or (not previous.isalnum() and previous not in ".,")
        while (
            boundary_ok
            and first_len <= 3
            and end + 4 <= len(text)
            and text[end] in "\u00a0\u202f\u2009 "
            and text[end + 1 : end + 4].isdigit()
            and (end + 4 >= len(text) or not text[end + 4].isdigit())
        ):
            run += text[end + 1 : end + 4]
            end += 4
        out.append(run)
        index = end
    return "".join(out)


# «1.500», «10.000», «1.234.567»: в выгрузках 1С и Excel точка — разделитель
# тысяч, а не десятичная. Дробные размеры с точкой («3.5», «2.25») шаблон
# не ловит: после точки там меньше трёх цифр.
_THOUSANDS_DOT = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(?![\d])")


def _dot_thousands(text: str) -> str:
    """«10.000 шт» → «10000 шт»: иначе заявка уменьшается в тысячу раз."""
    return _THOUSANDS_DOT.sub(lambda match: match.group(1).replace(".", ""), text)

# Единицы: длинные варианты раньше коротких, «м» — последним, иначе «мм» и «м2»
# прочитались бы как метры. Вместо \b в конце стоит (?!\w): «м²», «куб.м» и
# «м.п.» кончаются на не-слово, и граница слова после них не срабатывает вовсе —
# строка с такой единицей молча пропадала из заявки.
_UNIT_WORDS = (
    r"куб\.?\s*метр[а-я]*|куб\.?\s*м|м\.?\s*куб|м3|м³|"
    r"кв\.?\s*метр[а-я]*|кв\.?\s*м|м\.?\s*кв|м2|м²|"
    r"погонн[а-я]*\s*метр[а-я]*|пог\.?\s*метр[а-я]*|пог\.?\s*м|"
    r"м\.?\s*п|п\.?\s*м|"
    r"штук[а-я]*|шт|упак[а-я]*|уп|комплект[а-я]*|компл|"
    r"рулон[а-я]*|рул|пачк[а-я]*|пач|мешк[а-я]*|меш|канистр[а-я]*|лист[а-я]*|"
    r"килограмм[а-я]*|кг|литр[а-я]*|л|тонн[а-я]*|т|"
    r"метр[а-я]*|пар[а-я]*|м"
)
# «10 тыс. шт» — десять тысяч штук: «тыс.» — множитель, а не единица.
_THOUSAND = re.compile(r"^\s*тыс", re.IGNORECASE)
_QTY_UNIT = re.compile(
    rf"(?P<qty>\d{{1,7}}(?:[.,]\d{{1,3}})?)\s*(?P<thousand>тыс\.?\s*)?"
    rf"(?P<unit>{_UNIT_WORDS})(?!\w)",
    re.IGNORECASE,
)

# Канонические формы. Порядок важен: сначала площадь и объём, потом всё
# остальное, односимвольные — только точным совпадением (в _SINGLE_UNITS).
_UNIT_ALIASES = (
    (("кубм", "м3", "м³", "кубическ"), "м3"),
    (("квм", "м2", "м²", "квадратн"), "м2"),
    (("погонн", "погм", "пм", "мп", "пог", "метр"), "м"),
    (("миллиметр",), "мм"),
    (("сантиметр",), "см"),
    (("штук", "шт"), "шт"),
    (("упак", "уп"), "уп"),
    (("комплект", "компл"), "компл"),
    (("рулон", "рул"), "рул"),
    (("пачк", "пач"), "пач"),
    (("мешк", "меш"), "меш"),
    (("канистр",), "канистра"),
    (("лист",), "лист"),
    (("килограмм",), "кг"),
    (("литр",), "л"),
    (("тонн",), "т"),
    (("пар",), "пар"),
)

_SINGLE_UNITS = {
    "м": "м", "мм": "мм", "см": "см", "кг": "кг", "л": "л", "т": "т", "г": "г",
    "м2": "м2", "м²": "м2", "м3": "м3", "м³": "м3", "шт": "шт", "уп": "уп",
}


class LineExtractor(Protocol):
    def extract(self, text: str, *, max_lines: int = MAX_LINES) -> list[RfqLine]: ...


_TRAILING_QTY = re.compile(
    r"(?:(?P<dash>[—–]|\s-\s)\s*(?P<dash_qty>\d{1,7}(?:[.,]\d{1,3})?)"
    r"|\s(?P<plain_qty>\d{1,7}))\s*[.,;]?$"
)


def trailing_quantity(text: str) -> Decimal | None:
    """Количество без единицы: «Подрозетник 68 мм — 20» → 20.

    Клиент иногда пишет число, но не пишет «шт»/«м». Терять из-за этого всю
    строку нельзя: единицу подставит прайс, а подтвердит человек.

    Дробное число без тире количеством не считаем: «труба 25.4» — это размер,
    а не 25,4 метра. После тире («труба 25.4 — 1.5») дробь уже количество.
    """
    match = _TRAILING_QTY.search(text or "")
    if not match:
        return None
    raw = match.group("dash_qty") or match.group("plain_qty")
    try:
        value = quantity(Decimal(raw.replace(",", ".")))
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return value if value > 0 else None


def unit_multiplier(raw: str) -> int:
    """«10 тыс. шт» — это десять тысяч штук, а не десять: множитель единицы."""
    return 1000 if _THOUSAND.match(raw or "") else 1


def quantities_in(text: str) -> list[Decimal]:
    """Количества, которые в тексте стоят вместе с единицей: «200 шт», «10 тыс. шт».

    Нужно для проверки ответа LLM: если модель назвала число, которого в письме
    рядом с позицией нет, доверять ему нельзя.
    """
    found: list[Decimal] = []
    # Те же правила тысяч, что и при разборе: «10.000 шт» — это 10 000, а не 10.
    normalized = _dot_thousands(_glue_thousands(text or ""))
    for match in _QTY_UNIT.finditer(normalized):
        try:
            value = Decimal(match.group("qty").replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        found.append(quantity(value * (1000 if match.group("thousand") else 1)))
    return found


def canonical_unit(raw: str) -> str:
    """Приводит единицу к одной форме: «кв.м» и «м2» — это м2, а «мм» — не метры.

    Раньше сравнение шло по началу строки, и «мм» превращался в «м»,
    «тыс. шт» — в «т», а «м2» — в метры: количество молча менялось в 1000 раз
    и строка уходила в КП с чужой ценой за единицу.
    """
    low = re.sub(r"[\s.\u00a0\u202f]+", "", (raw or "").lower())
    if not low:
        return ""
    if low.startswith("тыс"):
        rest = low[3:]
        return canonical_unit(rest) if rest else "тыс"
    exact = _SINGLE_UNITS.get(low)
    if exact:
        return exact
    for variants, canonical in _UNIT_ALIASES:
        if any(low.startswith(variant) for variant in variants):
            return canonical
    return low


def _clean_name(segment: str, match: re.Match[str]) -> str:
    name = segment[: match.start()] + " " + segment[match.end() :]
    name = _BULLET.sub("", name)
    name = _LEAD_WORDS.sub("", name)
    name = _TRAIL.sub("", name)
    return re.sub(r"\s{2,}", " ", name).strip()


def _split_commas(segment: str) -> list[str]:
    """Режет список позиций по запятым, но не отрывает количество от названия.

    «уголок 50х50, 200 шт» — одна позиция (до запятой количества ещё нет),
    «200 шт уголок, 300 шт труба» — две (до запятой количество уже есть).
    """
    parts: list[str] = []
    start = 0
    for match in _COMMA.finditer(segment):
        before = segment[start : match.start()]
        after = segment[match.end() :]
        quantity_after = _QTY_UNIT.match(after) is not None
        if quantity_after and not _QTY_UNIT.search(before):
            # запятая отделяет название от его количества — оставляем в одной строке
            continue
        parts.append(before)
        start = match.end()
    parts.append(segment[start:])
    return [part for part in parts if part.strip()]


@dataclass(slots=True)
class RuleExtractor:
    """Разбирает строки вида «200 шт уголок 50х50» или «уголок 50х50 — 200 шт»."""

    max_lines: int = MAX_LINES
    skipped: list[str] = field(default_factory=list)

    def extract(self, text: str, *, max_lines: int | None = None) -> list[RfqLine]:
        limit = max_lines or self.max_lines
        self.skipped = []
        lines: list[RfqLine] = []
        seen: set[tuple[str, str, str]] = set()

        for segment in self._segments(_dot_thousands(_glue_thousands(text))):
            if len(lines) >= limit:
                break
            matches = list(_QTY_UNIT.finditer(segment))
            if not matches:
                if len(segment) >= 8:
                    self.skipped.append(segment[:200])
                continue

            # «Шпаклевка 25 кг — 10 меш»: количество — то, что после тире.
            # Иначе «25 кг» из названия уедет в количество и заявка подорожает.
            match = matches[0]
            dash = max(segment.rfind("—"), segment.rfind("–"), segment.rfind(" - "))
            if dash >= 0:
                after_dash = [m for m in matches if m.start() > dash]
                if after_dash:
                    match = after_dash[-1]

            name = _clean_name(segment, match)
            if len(name) < 3:
                name = segment.strip()
            if not _HAS_WORD.search(name):
                # «1 шт» или «2 x 3» — не позиция номенклатуры
                continue

            raw_qty = match.group("qty").replace(",", ".")
            unit = canonical_unit(match.group("unit"))
            multiplier = 1000 if match.group("thousand") else 1
            qty = quantity(Decimal(raw_qty) * multiplier)
            if qty <= 0:
                # «0 шт» — не позиция: строку оставляем человеку, но не роняем письмо
                self.skipped.append(segment[:200])
                continue
            key = (name.lower(), str(qty), unit)
            if key in seen:
                continue
            seen.add(key)

            lines.append(
                RfqLine(
                    idx=len(lines) + 1,
                    raw_text=name[:500],
                    qty=qty,
                    unit=unit,
                )
            )

        return lines

    @staticmethod
    def _segments(text: str) -> list[str]:
        segments: list[str] = []
        for chunk in _SEGMENT_SPLIT.split(text or ""):
            for sentence in _SENTENCE_SPLIT.split(chunk):
                for part in _SUB_SPLIT.split(sentence):
                    for piece in _split_commas(part):
                        cleaned = _BULLET.sub("", piece).strip()
                        cleaned = _GREETING.sub("", cleaned).strip()
                        if not cleaned:
                            continue
                        if _NOISE.match(cleaned):
                            continue
                        segments.append(cleaned[:500])
        return segments

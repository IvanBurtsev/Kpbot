"""Извлечение позиций из текста письма.

Правиловый экстрактор: детерминированный, без сети, без галлюцинаций.
Интерфейс LineExtractor позволяет подставить LLM-адаптер (structured output +
валидация той же pydantic-схемой), не меняя остальной пайплайн.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
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

# Единицы: длинные варианты раньше коротких.
_UNIT_WORDS = (
    r"штук[а-я]*|шт|упак[а-я]*|уп|комплект[а-я]*|компл|рулон[а-я]*|рул|пачк[а-я]*|пач|"
    r"мешк[а-я]*|меш|канистр[а-я]*|лист[а-я]*|погонн[а-я]*|метр[а-я]*|пог\.?\s?м|м\.?\s?п|м|кг|л|т"
)
_QTY_UNIT = re.compile(
    rf"(?P<qty>\d{{1,7}}(?:[.,]\d{{1,3}})?)\s*(?P<unit>{_UNIT_WORDS})\b",
    re.IGNORECASE,
)

_UNIT_MAP = (
    (("штук", "шт"), "шт"),
    (("упак", "уп"), "уп"),
    (("комплект", "компл"), "компл"),
    (("рулон", "рул"), "рул"),
    (("пачк", "пач"), "пач"),
    (("мешк", "меш"), "меш"),
    (("канистр",), "канистра"),
    (("лист",), "лист"),
    (("погонн", "метр"), "м"),
    (("пог", "м.п", "мп"), "м"),
    (("кг",), "кг"),
    (("л",), "л"),
    (("т",), "т"),
    (("м",), "м"),
)


class LineExtractor(Protocol):
    def extract(self, text: str, *, max_lines: int = MAX_LINES) -> list[RfqLine]: ...


def canonical_unit(raw: str) -> str:
    low = raw.lower().replace(".", "").replace(" ", "")
    for variants, canonical in _UNIT_MAP:
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
            qty = quantity(Decimal(raw_qty))
            if qty <= 0:
                # «0 шт» — не позиция: строку оставляем человеку, но не роняем письмо
                self.skipped.append(segment[:200])
                continue
            key = (name.lower(), raw_qty, unit)
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

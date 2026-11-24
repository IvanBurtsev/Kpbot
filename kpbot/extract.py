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
_SUB_SPLIT = re.compile(r",\s*(?=\d)|\s+и\s+(?=[А-Яа-яЁёA-Za-z\d])")
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

# Единицы: длинные варианты раньше коротких.
_QTY_UNIT = re.compile(
    r"(?P<qty>\d{1,7}(?:[.,]\d{1,3})?)\s*"
    r"(?P<unit>"
    r"штук[а-я]*|шт|упак[а-я]*|уп|комплект[а-я]*|компл|рулон[а-я]*|рул|пачк[а-я]*|пач|"
    r"мешк[а-я]*|меш|канистр[а-я]*|лист[а-я]*|погонн[а-я]*|метр[а-я]*|пог\.?\s?м|м\.?\s?п|м|кг|л|т"
    r")\b",
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

        for segment in self._segments(text):
            if len(lines) >= limit:
                break
            match = _QTY_UNIT.search(segment)
            if match is None:
                if len(segment) >= 8:
                    self.skipped.append(segment[:200])
                continue

            name = _clean_name(segment, match)
            if len(name) < 3:
                name = segment.strip()
            if not _HAS_WORD.search(name):
                # «1 шт» или «2 x 3» — не позиция номенклатуры
                continue

            raw_qty = match.group("qty").replace(",", ".")
            unit = canonical_unit(match.group("unit"))
            key = (name.lower(), raw_qty, unit)
            if key in seen:
                continue
            seen.add(key)

            lines.append(
                RfqLine(
                    idx=len(lines) + 1,
                    raw_text=name[:500],
                    qty=quantity(Decimal(raw_qty)),
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
                    cleaned = _BULLET.sub("", part).strip()
                    cleaned = _GREETING.sub("", cleaned).strip()
                    if not cleaned:
                        continue
                    if _NOISE.match(cleaned):
                        continue
                    segments.append(cleaned[:500])
        return segments
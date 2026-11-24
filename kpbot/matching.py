"""Матчинг свободного текста заявки с номенклатурой прайс-листа.

Скоринг объяснимый и считается на нашей стороне (не «модель сказала»):
    score = 0.55 * покрытие_токенов + 0.25 * строковое_сходство + 0.20 * числовой_бонус

Покрытие — доля токенов запроса, найденных в названии позиции (с учётом
русских словоформ по общему префиксу). Числовой бонус наказывает за
расхождение в размерах: «4.2х75» никогда не должно совпасть с «3.5х25».

В проде это дублируется pg_trgm/GIN на Postgres — контракт тот же.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .domain import LineStatus, PriceItem, RfqLine

STOPWORDS = frozenset(
    {
        "шт",
        "штук",
        "штука",
        "штуки",
        "уп",
        "упак",
        "упаковка",
        "компл",
        "рул",
        "рулон",
        "пач",
        "пачка",
        "пог",
        "м",
        "мм",
        "кг",
        "л",
        "т",
        "метр",
        "метра",
        "метров",
        "погонный",
        "погонных",
        "по",
        "для",
        "и",
        "в",
        "на",
        "с",
        # вежливость и служебные слова заявки — не часть названия позиции
        "нужно",
        "надо",
        "требуется",
        "прошу",
        "просим",
        "необходимо",
        "пожалуйста",
        "посчитайте",
        "посчитать",
        "рассчитайте",
        "сколько",
        "стоит",
        "стоить",
        "будет",
        # количества словами: не помогают сопоставлению названий
        "полторы",
        "полтора",
        "десяток",
        "двадцать",
        "тридцать",
        "сорок",
        "пятьдесят",
        "сто",
        "пара",
        "пару",
        "немного",
    }
)

_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\d.x]+", re.UNICODE)
_DIMENSION = re.compile(r"(?<=\d)\s*[x×*х]\s*(?=\d)")
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{3,}")


def normalize(text: str) -> str:
    """NFKC, нижний регистр, ё→е, единый разделитель размеров, без пунктуации."""
    value = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    value = _DIMENSION.sub("x", value)
    value = _NON_WORD.sub(" ", value)
    return _SPACES.sub(" ", value).strip(" .")


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t and t not in STOPWORDS]


def _stem(token: str) -> str:
    """Грубое усечение окончания: «штукатурки» и «штукатурка» дают одну основу."""
    return token[: max(4, len(token) - 2)]


def _one_edit_apart(left: str, right: str) -> bool:
    """Опечатка в одно изменение (замена, вставка, удаление) — «гафра» → «гофра»."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) == 1
    short, long = (left, right) if len(left) < len(right) else (right, left)
    index = 0
    while index < len(short) and short[index] == long[index]:
        index += 1
    return short[index:] == long[index + 1 :]


def _token_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    if _stem(left) == _stem(right):
        return True
    if len(left) >= 3 and len(right) >= 3:
        return left.startswith(right) or right.startswith(left)
    return False


def _token_match_weight(token: str, others: list[str]) -> float:
    """1.0 — точное/словоформенное совпадение, 0.8 — опечатка, 0.0 — нет."""
    if any(_token_equal(token, other) for other in others):
        return 1.0
    if len(token) >= 5 and any(len(other) >= 5 and _one_edit_apart(token, other) for other in others):
        return 0.8
    return 0.0


def _coverage(query_tokens: list[str], candidate_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    total = 0.0
    for token in query_tokens:
        total += _token_match_weight(token, candidate_tokens)
    return total / len(query_tokens)


def _numbers_bonus(query: str, candidate_norm: str) -> float:
    numeric = [t for t in query.split() if _DIGIT.search(t)]
    if not numeric:
        return 0.5  # размеров нет — нейтрально
    hits = sum(1 for token in numeric if token in candidate_norm.split() or token in candidate_norm)
    if hits == len(numeric):
        return 1.0
    if hits == 0:
        return 0.0
    return 0.5


def similarity(query: str, candidate: str) -> float:
    """0..1. Точное совпадение — 1.0, пустое — 0.0."""
    q = normalize(query)
    c = normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    q_tokens = tokens(q)
    c_tokens = tokens(c)
    coverage = _coverage(q_tokens, c_tokens)
    ratio = SequenceMatcher(None, q, c).ratio()
    bonus = _numbers_bonus(q, c)

    score = 0.55 * coverage + 0.25 * ratio + 0.20 * bonus
    return round(min(max(score, 0.0), 1.0), 4)


def best_match(raw_text: str, items: list[PriceItem]) -> tuple[PriceItem | None, float]:
    best_item: PriceItem | None = None
    best_score = 0.0
    for item in items:
        score = similarity(raw_text, item.name)
        if score > best_score:
            best_item, best_score = item, score
    return best_item, best_score


def _key(token: str) -> str:
    """Ключ индекса: первые 5 символов — устойчив к словоформам («саморез/саморезов»)."""
    return token[:5]


def _score(
    q: str,
    q_tokens: list[str],
    q_numbers: list[str],
    c_norm: str,
    c_tokens: list[str],
) -> float:
    coverage = _coverage(q_tokens, c_tokens)
    ratio = SequenceMatcher(None, q, c_norm).ratio()
    bonus = _numbers_bonus(q, c_norm)
    return round(min(max(0.55 * coverage + 0.25 * ratio + 0.20 * bonus, 0.0), 1.0), 4)


class PriceIndex:
    """Предрасчитанный индекс прайса: нормализация и инвертированный указатель по токенам.

    Нужен, чтобы на 10 000 позиций не сравнивать каждую строку заявки с каждой позицией.
    Кандидаты берутся только из позиций, делящих с запросом хотя бы один токен,
    затем отсекаются по верхней оценке скоринга (без дорогого SequenceMatcher).
    """

    __slots__ = ("items", "_norm", "_tokens", "_numbers", "_postings", "max_candidates")

    def __init__(self, items: list[PriceItem], *, max_candidates: int = 200) -> None:
        self.items = list(items)
        self.max_candidates = max_candidates
        self._norm: list[str] = []
        self._tokens: list[list[str]] = []
        self._numbers: list[list[str]] = []
        self._postings: dict[str, set[int]] = {}

        for idx, item in enumerate(self.items):
            norm = normalize(item.name)
            item_tokens = tokens(norm)
            self._norm.append(norm)
            self._tokens.append(item_tokens)
            self._numbers.append([t for t in item_tokens if _DIGIT.search(t)])
            for token in set(item_tokens):
                self._postings.setdefault(_key(token), set()).add(idx)

    def candidates(self, q_tokens: list[str], limit: int | None = None) -> list[int]:
        """Позиции, делящие с запросом хотя бы один токен, — самые «похожие» первыми."""
        limit = limit or self.max_candidates
        overlap: dict[int, int] = {}
        for token in q_tokens:
            for idx in self._postings.get(_key(token), ()):
                overlap[idx] = overlap.get(idx, 0) + 1
        if not overlap:
            return []
        ranked = sorted(overlap.items(), key=lambda pair: (-pair[1], pair[0]))
        return [idx for idx, _ in ranked[:limit]]

    def best(self, raw_text: str) -> tuple[PriceItem | None, float]:
        q = normalize(raw_text)
        if not q or not self.items:
            return None, 0.0

        q_tokens = tokens(q)
        # Запрос из одних цифр («1», «2 x 3») не может указывать на позицию прайса:
        # без слова любое совпадение — случайное.
        if not any(_WORD.search(token) for token in q_tokens):
            return None, 0.0
        q_numbers = [t for t in q_tokens if _DIGIT.search(t)]

        pool = self.candidates(q_tokens)
        if not pool and q_numbers:
            pool = [
                idx
                for idx, numbers in enumerate(self._numbers)
                if any(number in numbers for number in q_numbers)
            ][: self.max_candidates]
        if not pool:
            return None, 0.0

        best_idx = -1
        best_score = 0.0
        for idx in pool:
            candidate_tokens = self._tokens[idx]
            coverage = _coverage(q_tokens, candidate_tokens)
            # верхняя граница: ratio <= 1, числовой бонус <= 1
            if 0.55 * coverage + 0.45 <= best_score:
                continue
            score = _score(q, q_tokens, q_numbers, self._norm[idx], candidate_tokens)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx < 0:
            return None, 0.0
        return self.items[best_idx], best_score

    def __len__(self) -> int:
        return len(self.items)


def match_lines(
    lines: list[RfqLine],
    items: list[PriceItem] | PriceIndex,
    *,
    auto_threshold: float = 0.75,
    ambiguous_threshold: float = 0.50,
) -> list[RfqLine]:
    """Проставляет статус каждой строке. Ничего не додумывает: сомнительное — человеку."""
    index = items if isinstance(items, PriceIndex) else PriceIndex(items)

    for line in lines:
        if line.qty is None:
            line.status = LineStatus.INVALID
            line.note = "не распознано количество"
            continue

        item, score = index.best(line.raw_text)
        line.match_score = score

        if not line.unit:
            # Единица не названа, но позиция узнаваема: берём единицу из прайса
            # и обязательно просим подтверждение — угадывать молча нельзя.
            if item is not None and score >= ambiguous_threshold:
                line.matched_sku = item.sku
                line.matched_name = item.name
                line.unit = item.unit
                line.status = LineStatus.AMBIGUOUS
                line.note = "единица не указана — подставлена из прайса, нужно подтверждение"
            else:
                line.status = LineStatus.INVALID
                line.note = "не распознана единица измерения"
            continue

        if item is None or score < ambiguous_threshold:
            line.status = LineStatus.NO_MATCH
            line.note = "позиция не найдена в прайс-листе"
            continue

        line.matched_sku = item.sku
        line.matched_name = item.name

        if item.unit != line.unit:
            line.status = LineStatus.AMBIGUOUS
            line.note = f"единица не совпадает: в письме «{line.unit}», в прайсе «{item.unit}»"
            continue

        if score >= auto_threshold:
            line.status = LineStatus.OK
            line.note = ""
        else:
            line.status = LineStatus.AMBIGUOUS
            line.note = "похожая позиция, нужно подтверждение человека"

    return lines
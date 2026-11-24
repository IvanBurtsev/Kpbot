"""Замер скорости матчинга на прайсах разного размера.

Запуск:  python scripts/bench.py
Печатает время сборки индекса и время обработки строк заявки на 100 / 1 000 / 10 000 позиций.
"""

from __future__ import annotations

import random
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kpbot.domain import PriceItem, RfqLine
from kpbot.matching import PriceIndex, match_lines

BASE_NAMES = [
    "Уголок монтажный {n}х{n} мм стальной",
    "Кабель ВВГнг-LS {n}х{a}.5",
    "Труба ППР {n} мм PN20",
    "Саморез по дереву {a}.2х{n} мм",
    "Цемент М{n} {a} кг",
    "Плита OSB {n} мм 1250х2500",
    "Кран шаровой {a}/2 дюйма латунь",
    "Профиль ПН-{n}/{a} 3 м",
    "Грунтовка глубокого проникновения {n} л",
    "Пена монтажная {n} мл",
]

QUERIES = [
    "уголок 50х50",
    "кабель ВВГнг 3х2.5",
    "труба ППР 25",
    "саморез 4.2х75",
    "цемент М500",
    "плита OSB 9 мм",
    "кран шаровой 1/2",
    "профиль ПН-27/28",
    "грунтовка 10 л",
    "пена монтажная 750",
]

SIZES = (50, 100, 200, 300, 500, 750, 1000, 1500, 2500, 4000, 6000, 10000)


def build_price(size: int, seed: int = 7) -> list[PriceItem]:
    rng = random.Random(seed)
    items: list[PriceItem] = []
    # первые позиции гарантированно совпадают с запросами — иначе замер меряет промахи
    for index, query in enumerate(QUERIES):
        items.append(PriceItem(sku=f"BASE-{index:03d}", name=query.title(), unit="шт", price=Decimal("100.00")))

    while len(items) < size:
        index = len(items)
        template = BASE_NAMES[index % len(BASE_NAMES)]
        name = template.format(
            n=rng.choice([16, 20, 25, 32, 40, 50, 75, 100, 500, 750]),
            a=rng.choice([1, 2, 3, 5, 6, 9]),
        )
        items.append(
            PriceItem(sku=f"SKU-{index:06d}", name=f"{name} №{index}", unit="шт", price=Decimal("100.00"))
        )
    return items


def run(size: int, lines_count: int = 20) -> dict:
    items = build_price(size)

    started = time.perf_counter()
    index = PriceIndex(items)
    build_ms = (time.perf_counter() - started) * 1000

    lines = [
        RfqLine(idx=i + 1, raw_text=QUERIES[i % len(QUERIES)], qty=Decimal("1"), unit="шт")
        for i in range(lines_count)
    ]

    started = time.perf_counter()
    match_lines(lines, index)
    total_ms = (time.perf_counter() - started) * 1000

    matched = sum(1 for line in lines if line.status.value == "ok")
    return {
        "size": size,
        "build_ms": round(build_ms, 1),
        "match_ms": round(total_ms, 1),
        "per_line_ms": round(total_ms / lines_count, 2),
        "lines_per_sec": round(lines_count / (total_ms / 1000), 1) if total_ms else 0.0,
        "matched": f"{matched}/{lines_count}",
    }


def main() -> int:
    print(
        f"{'позиций':>8} {'индекс, мс':>11} {'20 строк, мс':>13} {'строка, мс':>11} "
        f"{'строк/с':>9} {'в прайсе':>9}"
    )
    for size in SIZES:
        row = run(size)
        print(
            f"{row['size']:>8} {row['build_ms']:>11} {row['match_ms']:>13} "
            f"{row['per_line_ms']:>11} {row['lines_per_sec']:>9} {row['matched']:>9}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
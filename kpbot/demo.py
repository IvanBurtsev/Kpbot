"""Демо-стенд: заявки, собранные из реальной номенклатуры клиента.

Смысл: чтобы на звонке показывать разбор на тех же позициях, которые у клиента
в 1С, а не на выдуманном прайсе. Письма генерируются из загруженного прайса,
поэтому все позиции заведомо существуют.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .domain import PriceItem

COMPANIES: tuple[str, ...] = (
    "ООО «СтройМонтаж» <zakaz@stroymontazh.demo.local>",
    "ООО «Гарант-Строй» <snab@garant-stroy.demo.local>",
    "ИП Сергеев <sergeev.demo.local@mail.example>",
    "ООО «РемонтСервис» <zakupki@remont-service.demo.local>",
    "ООО «ТеплоДом» <supply@teplodom.demo.local>",
    "ООО «МастерБыт» <info@masterbyt.demo.local>",
)

SUBJECTS: tuple[str, ...] = (
    "Заявка на материалы",
    "Заявка на поставку",
    "Нужен расчёт",
    "Прошу рассчитать",
    "Заказ на следующую неделю",
)

QUANTITIES: tuple[int, ...] = (5, 10, 12, 20, 25, 40, 50, 60, 100, 120, 150, 200)

# позиция вне прайса: проверяем, что сервис не выдумывает цену
STRANGERS: tuple[str, ...] = (
    "монтаж кабель-канала",
    "доставка до объекта",
    "подрозетник скрытой установки",
    "услуги по разгрузке",
)


@dataclass(slots=True)
class DemoLetter:
    sender: str
    subject: str
    body: str


def _short(name: str) -> str:
    """Укороченный вариант названия — так пишут живые снабженцы."""
    words = name.split()
    return " ".join(words[:3]) if len(words) > 3 else name


def _typo(name: str) -> str:
    if len(name) < 6:
        return name
    position = max(1, len(name) // 2 - 1)
    return name[:position] + name[position + 1 :]


def _line(item: PriceItem, qty: int, style: str) -> str:
    if style == "short":
        return f"{_short(item.name)} — {qty} {item.unit}"
    if style == "typo":
        return f"{_typo(item.name)} — {qty} {item.unit}"
    return f"{item.name} — {qty} {item.unit}"


def build_letters(items: list[PriceItem], count: int = 6, seed: int = 42) -> list[DemoLetter]:
    """Собирает письма из номенклатуры: точные названия, сокращения, опечатки, лишнее."""
    if not items:
        raise ValueError("для демо-стенда нужен непустой прайс")
    rng = random.Random(seed)
    letters: list[DemoLetter] = []
    styles = ("exact", "exact", "short", "typo", "exact", "short")

    for index in range(max(1, count)):
        company = COMPANIES[index % len(COMPANIES)]
        picked = rng.sample(items, k=min(len(items), rng.choice((3, 4, 5))))
        style = styles[index % len(styles)]
        lines = [
            f"{position}. {_line(item, rng.choice(QUANTITIES), style)}"
            for position, item in enumerate(picked, start=1)
        ]
        # в часть писем добавляем позицию, которой нет в прайсе
        if index % 2 == 1:
            lines.append(f"{len(lines) + 1}. {rng.choice(STRANGERS)} — 1 услуга")

        body = "Добрый день!\n\nПрошу рассчитать:\n" + "\n".join(lines) + "\n\nСпасибо."
        letters.append(
            DemoLetter(
                sender=company.split(" <")[0] + " <" + company.split("<")[1],
                subject=SUBJECTS[index % len(SUBJECTS)],
                body=body,
            )
        )
    return letters


def sender_address(sender: str) -> str:
    """«ООО «СтройМонтаж» <zakaz@...>» → «zakaz@...»."""
    from .mailparse import sender_address as parse_sender

    return parse_sender(sender)

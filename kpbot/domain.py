"""Доменный слой: деньги, сущности, расчёт. Никакого I/O.

Главное правило: модель/экстрактор извлекает, но НЕ назначает цену.
Цену считает только код по прайс-листу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.001")


class ValidationError(ValueError):
    """Нарушение инварианта. Ловится на границе и превращается в 422."""


def money(value: object) -> Decimal:
    """Деньги — только Decimal, округление half-up до копейки."""
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValidationError(f"некорректная денежная сумма: {value!r}") from exc


def quantity(value: object) -> Decimal:
    try:
        return Decimal(str(value)).quantize(QTY_STEP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"некорректное количество: {value!r}") from exc


class LineStatus(str, Enum):
    OK = "ok"                 # найдено в прайсе однозначно, можно считать
    AMBIGUOUS = "ambiguous"   # похоже, но не точно — человеку
    NO_MATCH = "no_match"     # не найдено
    INVALID = "invalid"       # нет количества/единицы


class RfqStatus(str, Enum):
    RECEIVED = "received"
    PARSED = "parsed"
    PRICED = "priced"
    DRAFT_READY = "draft_ready"
    HUMAN_REVIEW = "human_review"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


# Разрешённые переходы. Терминальные sent не меняется; rejected можно вернуть
# в работу правкой строк. Повторный переход в тот же статус — не ошибка.
RFQ_TRANSITIONS: dict[RfqStatus, frozenset[RfqStatus]] = {
    RfqStatus.RECEIVED: frozenset(
        {RfqStatus.PARSED, RfqStatus.DRAFT_READY, RfqStatus.HUMAN_REVIEW, RfqStatus.REJECTED}
    ),
    RfqStatus.PARSED: frozenset(
        {RfqStatus.PRICED, RfqStatus.DRAFT_READY, RfqStatus.HUMAN_REVIEW, RfqStatus.REJECTED}
    ),
    RfqStatus.PRICED: frozenset({RfqStatus.DRAFT_READY, RfqStatus.HUMAN_REVIEW, RfqStatus.REJECTED}),
    RfqStatus.DRAFT_READY: frozenset({RfqStatus.APPROVED, RfqStatus.HUMAN_REVIEW, RfqStatus.REJECTED}),
    RfqStatus.HUMAN_REVIEW: frozenset({RfqStatus.DRAFT_READY, RfqStatus.APPROVED, RfqStatus.REJECTED}),
    RfqStatus.APPROVED: frozenset(
        {RfqStatus.SENT, RfqStatus.DRAFT_READY, RfqStatus.HUMAN_REVIEW, RfqStatus.REJECTED}
    ),
    RfqStatus.SENT: frozenset(),
    RfqStatus.REJECTED: frozenset({RfqStatus.HUMAN_REVIEW, RfqStatus.DRAFT_READY}),
}


def check_transition(current: RfqStatus | str, new: RfqStatus | str) -> None:
    """Проверяет переход и бросает ValidationError, если он запрещён."""
    try:
        current_status = RfqStatus(current)
        new_status = RfqStatus(new)
    except ValueError as exc:
        raise ValidationError(f"неизвестный статус заявки: {exc}") from exc
    if current_status == new_status:
        return
    if new_status not in RFQ_TRANSITIONS[current_status]:
        raise ValidationError(
            f"нельзя перевести заявку из «{current_status.value}» в «{new_status.value}»"
        )


@dataclass(frozen=True, slots=True)
class PriceItem:
    sku: str
    name: str
    unit: str
    price: Decimal
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if not self.sku.strip():
            raise ValidationError("пустой SKU")
        if self.price < 0:
            raise ValidationError(f"отрицательная цена у {self.sku}")


@dataclass(slots=True)
class RfqLine:
    idx: int
    raw_text: str
    qty: Decimal | None = None
    unit: str | None = None
    matched_sku: str | None = None
    match_score: float = 0.0
    matched_name: str = ""
    status: LineStatus = LineStatus.INVALID
    note: str = ""


@dataclass(frozen=True, slots=True)
class QuoteLine:
    sku: str
    name: str
    unit: str
    qty: Decimal
    price: Decimal
    amount: Decimal


@dataclass(slots=True)
class Quote:
    lines: list[QuoteLine]
    subtotal: Decimal
    vat: Decimal
    delivery: Decimal
    total: Decimal
    vat_rate: Decimal
    requires_review: bool
    unmatched: int
    excluded: list[str] = field(default_factory=list)
    # Сквозной номер КП в базе клиента: бухгалтерии нужен «№ 12 от 20.11.2026»,
    # а не первые 8 символов uuid.
    number: int | None = None


def price_quote(
    lines: list[RfqLine],
    price_by_sku: dict[str, PriceItem],
    *,
    vat_rate: Decimal = Decimal("0.22"),
    delivery: Decimal = Decimal("0.00"),
    max_qty: Decimal = Decimal("1000000"),
) -> Quote:
    """Считает КП строго по подтверждённым строкам.

    Строки не в статусе OK в расчёт не попадают и перечисляются в excluded —
    «не нашёл в прайсе» никогда не превращается в выдуманную цену.
    """
    if vat_rate < 0 or vat_rate > 1:
        raise ValidationError("ставка НДС должна быть в диапазоне 0..1")

    quoted: list[QuoteLine] = []
    excluded: list[str] = []

    for line in lines:
        if line.status is not LineStatus.OK or not line.matched_sku:
            excluded.append(line.raw_text)
            continue

        item = price_by_sku.get(line.matched_sku)
        if item is None:  # прайс изменился между матчингом и расчётом
            excluded.append(line.raw_text)
            continue

        if line.qty is None:
            excluded.append(line.raw_text)
            continue

        qty = quantity(line.qty)
        if qty <= 0:
            # Одна битая строка не должна выбрасывать всё письмо: отдаём человеку.
            excluded.append(line.raw_text)
            continue
        if qty > max_qty:
            raise ValidationError(f"количество больше допустимого: {line.raw_text!r}")

        price = money(item.price)
        quoted.append(
            QuoteLine(
                sku=item.sku,
                name=item.name,
                unit=item.unit,
                qty=qty,
                price=price,
                # сумма считается от уже округлённой цены, иначе «0,34 × 3 = 1,01»
                amount=money(price * qty),
            )
        )

    subtotal = money(sum((ln.amount for ln in quoted), Decimal("0")))
    vat = money(subtotal * vat_rate)
    delivery_money = money(max(delivery, Decimal("0")))
    total = money(subtotal + vat + delivery_money)

    return Quote(
        lines=quoted,
        subtotal=subtotal,
        vat=vat,
        delivery=delivery_money,
        total=total,
        vat_rate=vat_rate,
        requires_review=bool(excluded) or not quoted,
        unmatched=len(excluded),
        excluded=excluded,
    )


def build_reply_subject(subject: str) -> str:
    """Тема ответа: Re: <исходная>, без накопления 'Re: Re:'."""
    clean = subject.strip() or "Заявка"
    lowered = clean.lower()
    if lowered.startswith("re:"):
        return clean
    return f"Re: {clean}"


def status_for(quote: Quote) -> RfqStatus:
    return RfqStatus.HUMAN_REVIEW if quote.requires_review else RfqStatus.DRAFT_READY
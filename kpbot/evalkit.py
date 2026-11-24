"""Замер качества разбора на размеченном наборе.

Считает то, что нужно для пилота: сколько строк разобралось без человека,
сколько позиций потеряно, сколько добавлено лишнего, совпала ли сумма.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .domain import Quote, RfqLine
from .evalcases import CASES
from .pipeline import Pipeline


@dataclass(slots=True)
class CaseResult:
    name: str
    lines: int = 0
    priced: int = 0
    expected: int = 0
    found: int = 0
    extra: int = 0
    missing: list[str] = field(default_factory=list)
    total: Decimal = Decimal("0")
    expected_total: Decimal = Decimal("0")
    is_request: bool = True
    ready: bool = False


def _qty(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def run_case(pipeline: Pipeline, case: dict) -> CaseResult:
    lines, quote, is_request = pipeline.analyze(case["text"])
    result = CaseResult(
        name=case["name"],
        lines=len(lines),
        priced=len(quote.lines),
        expected=len(case["expected"]),
        is_request=is_request,
        total=quote.total,
        ready=not quote.requires_review,
    )

    priced_by_sku = {line.sku: line for line in quote.lines}
    for sku, expected_qty in case["expected"]:
        line = priced_by_sku.get(sku)
        if line is None:
            result.missing.append(sku)
            continue
        if _qty(line.qty) != _qty(expected_qty):
            result.missing.append(f"{sku}×{expected_qty} (в расчёте {line.qty})")
            continue
        result.found += 1

    expected_skus = {sku for sku, _ in case["expected"]}
    result.extra = len([line for line in quote.lines if line.sku not in expected_skus])
    result.expected_total = _expected_total(pipeline, case["expected"])
    return result


def _expected_total(pipeline: Pipeline, expected: list[tuple[str, str]]) -> Decimal:
    prices = {item.sku: item for item in pipeline.price_index().items}
    subtotal = Decimal("0")
    for sku, qty in expected:
        item = prices.get(sku)
        if item is not None:
            subtotal += item.price * _qty(qty)
    vat = subtotal * pipeline.cfg.vat_rate
    return (subtotal + vat).quantize(Decimal("0.01"))


@dataclass(slots=True)
class Summary:
    cases: list[CaseResult]
    expected: int
    found: int
    extra: int
    lines: int
    priced: int
    auto_rate: float
    recall: float
    precision: float
    request_accuracy: int
    request_total: int
    ready: int
    requests: int = 0
    extractor: str = "rules"


def format_report(summary: Summary, *, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"{'кейс':44} {'строк':>6} {'авто':>5} {'ожид':>5} {'найд':>5} {'лишн':>5} {'заявка':>7}")
    lines.append("-" * 86)
    for case in summary.cases:
        flag = "да" if case.is_request else "нет"
        lines.append(
            f"{case.name[:44]:44} {case.lines:>6} {case.priced:>5} {case.expected:>5} "
            f"{case.found:>5} {case.extra:>5} {flag:>7}"
        )
        if verbose:
            for item in case.missing:
                lines.append(f"    потеряно: {item}")
    lines.append("-" * 86)
    lines.append(
        f"итого: позиций ожидалось {summary.expected}, найдено {summary.found}, лишних {summary.extra}"
    )
    lines.append(
        f"строк в письмах {summary.lines}, разобрано без человека {summary.priced} "
        f"({summary.auto_rate:.0%})"
    )
    lines.append(
        f"полнота (recall) {summary.recall:.0%}, точность (precision) {summary.precision:.0%}"
    )
    lines.append(
        f"определение «заявка / не заявка»: {summary.request_accuracy} из {summary.request_total}"
    )
    lines.append(
        f"готовы к отправке без правок: {summary.ready} из {summary.requests} заявок · "
        f"экстрактор: {summary.extractor}"
    )
    return "\n".join(lines)


def evaluate(pipeline: Pipeline, cases: tuple[dict, ...] = CASES) -> Summary:
    results = [run_case(pipeline, case) for case in cases]

    expected = sum(r.expected for r in results)
    found = sum(r.found for r in results)
    extra = sum(r.extra for r in results)
    lines = sum(r.lines for r in results)
    priced = sum(r.priced for r in results)

    request_ok = 0
    for case, result in zip(cases, results):
        if result.is_request == bool(case.get("is_request", bool(case["expected"]))):
            request_ok += 1

    requests = [result for result in results if result.is_request]

    return Summary(
        cases=results,
        expected=expected,
        found=found,
        extra=extra,
        lines=lines,
        priced=priced,
        auto_rate=round(priced / lines, 3) if lines else 0.0,
        recall=round(found / expected, 3) if expected else 1.0,
        precision=round(found / (found + extra), 3) if (found + extra) else 1.0,
        request_accuracy=request_ok,
        request_total=len(results),
        ready=sum(1 for result in requests if result.ready),
        requests=len(requests),
        extractor="llm" if type(pipeline.extractor).__name__ == "LlmExtractor" else "rules",
    )
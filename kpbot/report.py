"""Отчёт пилота: цифры, которые показывают клиенту после двух недель.

Пилотное соглашение обещает «количество заявок, долю строк без человека,
среднее время подготовки КП». Этот модуль собирает обещанное в один HTML,
который можно распечатать или отправить клиенту.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .render import render_template


def _money(value: object) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except Exception:
        return str(value)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def build_pilot_report(store, *, days: int = 30, company: str = "", period_note: str = "") -> str:
    data = store.report(days)
    unmatched = store.unmatched_report(days)

    quotes = int(data.get("quotes") or 0)
    median = data.get("median_seconds")
    baseline_minutes = 20
    saved_hours = None
    if quotes and median is not None:
        saved_hours = round(
            max(0.0, (quotes * baseline_minutes - quotes * float(median) / 60) / 60), 1
        )

    return render_template(
        "report.html",
        generated_at=datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
        company=company,
        period_note=period_note,
        days=int(days),
        data=data,
        unmatched=unmatched,
        total_sum=_money(data.get("total_sum")),
        llm_cost=_money(data.get("llm_cost")),
        saved_hours=saved_hours,
        baseline_minutes=baseline_minutes,
    )

"""Рендер КП: HTML (Jinja2, autoescape), DOCX (python-docx) и PDF через headless-браузер.

PDF-адаптер сознательно необязательный: если браузер в окружении недоступен,
КП всё равно отдаётся в HTML (печать в PDF из браузера) и в DOCX (правится в Word).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import BASE_DIR, settings

log = logging.getLogger("kpbot.render")

_VENDOR = BASE_DIR / "vendor"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

_env = Environment(
    loader=FileSystemLoader(str(settings.templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def rub(value: object) -> str:
    """1234.5 -> '1 234,50'."""
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except Exception:
        return str(value)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def _qty(value: object) -> str:
    """1000.000 -> '1000', 2.500 -> '2,5'."""
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except Exception:
        return str(value)
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


_env.filters["rub"] = rub
_env.filters["qty"] = _qty


def render_template(name: str, /, **context: object) -> str:
    return _env.get_template(name).render(**context)


def write_html(path: Path, html: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _browser_candidates() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(settings.edge_path),
    ]
    return [path for path in candidates if path.exists()]


def html_to_pdf(html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    """Печатает HTML в PDF первым доступным браузером. Ошибка не роняет пайплайн."""
    browsers = _browser_candidates()
    if not browsers:
        return False, "браузер для печати PDF не найден"

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()

    profile = settings.data_dir / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)

    last_error = "неизвестная ошибка"
    for browser in browsers:
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - аргументы фиксированы, shell=False
                command,
                capture_output=True,
                timeout=settings.pdf_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = "печать PDF не уложилась в таймаут"
            continue
        except OSError as exc:
            last_error = f"не удалось запустить {browser.name}: {exc}"
            continue
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        if pdf_path.exists() and pdf_path.stat().st_size > 1024:
            return True, "ok"
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        last_error = f"{browser.name}: код {completed.returncode}" + (
            f", {stderr[-1][:160]}" if stderr else ""
        )

    return False, last_error


def render_quote_docx(
    path: Path,
    *,
    company: dict[str, str],
    client: dict[str, str],
    rfq: dict[str, str],
    quote: object,
    valid_days: int,
) -> Path:
    """КП в DOCX — его можно править в Word и печатать в PDF оттуда."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(1.6)
    section.right_margin = Cm(1.2)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    title = doc.add_paragraph()
    run = title.add_run(company["name"])
    run.bold = True
    run.font.size = Pt(14)

    for text in (
        f"ИНН {company['inn']} · КПП {company['kpp']}",
        company["address"],
        f"{company['phone']} · {company['email']}",
    ):
        doc.add_paragraph(text).runs[0].font.size = Pt(8)

    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    head_run = head.add_run(f"Коммерческое предложение № {rfq['id'][:8].upper()}")
    head_run.bold = True
    head_run.font.size = Pt(13)

    meta = doc.add_paragraph()
    meta.add_run(f"Кому: {client['sender']}\nПо заявке: {client['subject']}")

    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    headers = ("№", "Наименование", "Артикул", "Ед.", "Кол-во", "Цена, ₽", "Сумма, ₽")
    for index, label in enumerate(headers):
        cell = table.rows[0].cells[index]
        cell.text = label
        for paragraph in cell.paragraphs:
            for cell_run in paragraph.runs:
                cell_run.bold = True
                cell_run.font.size = Pt(8)

    for number, line in enumerate(quote.lines, start=1):
        cells = table.add_row().cells
        values = (
            str(number),
            line.name,
            line.sku,
            line.unit,
            _qty(line.qty),
            rub(line.price),
            rub(line.amount),
        )
        for index, value in enumerate(values):
            cells[index].text = value
            for paragraph in cells[index].paragraphs:
                for cell_run in paragraph.runs:
                    cell_run.font.size = Pt(9)

    doc.add_paragraph()
    for label, value in (
        ("Сумма без НДС", f"{rub(quote.subtotal)} ₽"),
        (f"НДС {int(round(quote.vat_rate * 100))}%", f"{rub(quote.vat)} ₽"),
        ("Доставка", f"{rub(quote.delivery)} ₽"),
    ):
        row = doc.add_paragraph()
        row.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        row.add_run(f"{label}: {value}")

    total = doc.add_paragraph()
    total.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    total_run = total.add_run(f"Итого к оплате: {rub(quote.total)} ₽")
    total_run.bold = True
    total_run.font.size = Pt(12)

    if quote.excluded:
        note = doc.add_paragraph()
        note.add_run(
            f"Позиции, требующие уточнения ({len(quote.excluded)}): "
            "в расчёт не включены, цену по ним не подтверждаем."
        ).italic = True
        for item in quote.excluded:
            doc.add_paragraph(f"— {item}", style="List Bullet")

    doc.add_paragraph(
        f"Предложение действительно {valid_days} календарных дней с даты выставления."
    ).runs[0].font.size = Pt(9)
    doc.add_paragraph("Срок поставки и условия оплаты согласуются отдельно.").runs[0].font.size = Pt(9)

    doc.add_paragraph()
    sign = doc.add_paragraph()
    sign.add_run("\n\n" + company["signer"] + "\n_______________________ / подпись, печать")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
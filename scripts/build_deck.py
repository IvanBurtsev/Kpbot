"""Собирает презентацию kpbot для портфолио и продажи.

    python scripts/build_deck.py

Если в data/presentation/shots лежат настоящие скриншоты (их снимает
scripts/capture_shots.py), они вставляются в слайды про интерфейс.
Иначе рисуются схемы экранов — колода остаётся честной и читаемой.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "presentation" / "shots"
OUT = ROOT / "presentation" / "kpbot-deck.pptx"

W, H = 13.333, 7.5

INK = RGBColor(0x0F, 0x17, 0x2A)
MUTED = RGBColor(0x64, 0x74, 0x8B)
ACCENT = RGBColor(0x25, 0x63, 0xEB)
ACCENT_SOFT = RGBColor(0xDB, 0xEA, 0xFE)
GOLD = RGBColor(0xC9, 0xA2, 0x27)
GREEN = RGBColor(0x15, 0x80, 0x3D)
GREEN_SOFT = RGBColor(0xDC, 0xFC, 0xE7)
RED = RGBColor(0xB9, 0x1C, 0x1C)
RED_SOFT = RGBColor(0xFE, 0xE2, 0xE2)
AMBER = RGBColor(0x92, 0x40, 0x0E)
AMBER_SOFT = RGBColor(0xFE, 0xF3, 0xC7)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BG = RGBColor(0xF8, 0xFA, 0xFC)
LINE = RGBColor(0xE2, 0xE8, 0xF0)
INK_SOFT = RGBColor(0x1E, 0x29, 0x3B)

FONT = "Segoe UI"


def text(
    slide, x, y, w, h, value, *, size=16, bold=False, color=INK, align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP, italic=False, spacing=1.0, font=FONT,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    lines = str(value).split("\n")
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.line_spacing = spacing
        run = paragraph.add_run()
        run.text = line
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color
    return box


def rect(slide, x, y, w, h, fill, *, line=None, radius=0.12, shadow=False):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line
        shape.line.width = Pt(0.75)
    if radius:
        try:
            shape.adjustments[0] = radius
        except (IndexError, ValueError):
            pass
    shape.shadow.inherit = False
    return shape


def chip(slide, x, y, w, text_value, *, fill=GREEN_SOFT, color=GREEN, size=11):
    rect(slide, x, y, w, 0.34, fill, radius=0.5)
    text(slide, x, y + 0.06, w, 0.24, text_value, size=size, bold=True, color=color, align=PP_ALIGN.CENTER)


def header(slide, kicker, title, subtitle=""):
    text(slide, 0.85, 0.55, 11.6, 0.35, kicker.upper(), size=11, bold=True, color=ACCENT)
    text(slide, 0.85, 0.9, 11.6, 0.7, title, size=32, bold=True, color=INK)
    if subtitle:
        text(slide, 0.85, 1.62, 11.6, 0.5, subtitle, size=15, color=MUTED)


def footer(slide, number):
    text(slide, 0.85, 7.02, 6, 0.3, "kpbot · заявка из письма → КП", size=10, color=MUTED)
    text(slide, 11.9, 7.02, 0.6, 0.3, f"{number:02d}", size=10, bold=True, color=MUTED, align=PP_ALIGN.RIGHT)


def new_slide(prs, bg=BG):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    rect(slide, 0, 0, W, H, bg, radius=0, line=None)
    return slide


def table(slide, x, y, w, rows, col_widths, *, header_fill=INK, size=12, row_h=0.42, header_h=0.5):
    shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(x), Inches(y), Inches(w), Inches(len(rows) * row_h))
    shape.height = Inches(header_h + (len(rows) - 1) * row_h)
    grid = shape.table
    for index, width in enumerate(col_widths):
        grid.columns[index].width = Inches(width)
    for r, row in enumerate(rows):
        grid.rows[r].height = Inches(header_h if r == 0 else row_h)
        for c, value in enumerate(row):
            cell = grid.cell(r, c)
            cell.margin_left = cell.margin_right = Inches(0.1)
            cell.margin_top = cell.margin_bottom = Inches(0.05)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            frame = cell.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            run = paragraph.add_run()
            run.text = str(value)
            run.font.name = FONT
            run.font.size = Pt(size)
            run.font.bold = r == 0
            run.font.color.rgb = WHITE if r == 0 else INK
            cell.fill.solid()
            cell.fill.fore_color.rgb = header_fill if r == 0 else (WHITE if r % 2 else BG)
    return grid


def browser(slide, x, y, w, h, url="127.0.0.1:8000/ui"):
    rect(slide, x, y, w, h, WHITE, line=LINE, radius=0.03)
    rect(slide, x, y, w, 0.34, RGBColor(0xEE, 0xF2, 0xF7), radius=0.03, line=None)
    for index, color in enumerate((RGBColor(0xEF, 0x44, 0x44), RGBColor(0xF5, 0x9E, 0x0B), RGBColor(0x22, 0xC5, 0x5E))):
        dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x + 0.14 + index * 0.18), Inches(y + 0.11), Inches(0.12), Inches(0.12))
        dot.fill.solid()
        dot.fill.fore_color.rgb = color
        dot.line.fill.background()
        dot.shadow.inherit = False
    rect(slide, x + 0.72, y + 0.06, w - 0.9, 0.22, WHITE, radius=0.5, line=LINE)
    text(slide, x + 0.82, y + 0.08, w - 1.1, 0.18, url, size=9, color=MUTED)


def picture_or_scheme(slide, filename, x, y, w, h, scheme, caption):
    path = SHOTS / filename
    if path.exists():
        picture = slide.shapes.add_picture(str(path), Inches(x), Inches(y), width=Inches(w))
        max_h = Inches(h)
        if picture.height > max_h:
            ratio = max_h / picture.height
            picture.height = max_h
            picture.width = int(picture.width * ratio)
        text(slide, x, y + h + 0.08, w, 0.25, caption, size=10, color=MUTED)
        return True
    scheme(slide, x, y, w, h)
    text(slide, x, y + h + 0.08, w, 0.3,
         caption + " · схема; настоящий скриншот снимается scripts/capture_shots.py",
         size=9, color=MUTED)
    return False


def inbox_scheme(slide, x, y, w, h):
    browser(slide, x, y, w, h)
    top = y + 0.34
    rect(slide, x, top, 2.2, h - 0.34, RGBColor(0x0F, 0x17, 0x2A), radius=0)
    text(slide, x + 0.2, top + 0.25, 1.8, 0.3, "kpbot", size=15, bold=True, color=WHITE)
    for index, item in enumerate(("Заявки", "Прайс", "Сводка", "Админка")):
        text(slide, x + 0.2, top + 0.8 + index * 0.42, 1.8, 0.3, item, size=11, color=WHITE if index == 0 else RGBColor(0x94, 0xA3, 0xB8))
    text(slide, x + 2.5, top + 0.25, 5.4, 0.4, "Входящие заявки", size=18, bold=True, color=INK)
    text(slide, x + 2.5, top + 0.68, 5.4, 0.3, "6 заявок · 4 готовы к отправке", size=11, color=MUTED)
    rows = (
        ("Заявка на материалы", "СтройМонтаж", "70 392,00 ₽", "готово"),
        ("Уголок и кабель", "ООО «Вектор»", "16 836,00 ₽", "готово"),
        ("Заявка № 214", "ИП Сергеев", "36 892,80 ₽", "проверить"),
        ("Список на неделю", "МеталлБаза", "—", "не заявка"),
    )
    for index, (title, sender, total, status) in enumerate(rows):
        row_y = top + 1.1 + index * 0.62
        rect(slide, x + 2.5, row_y, w - 2.8, 0.52, WHITE, line=LINE, radius=0.08)
        text(slide, x + 2.65, row_y + 0.09, 3.2, 0.32, title, size=11, bold=True, color=INK)
        text(slide, x + 5.6, row_y + 0.11, 1.7, 0.3, sender, size=10, color=MUTED)
        text(slide, x + 7.2, row_y + 0.11, 1.4, 0.3, total, size=10, bold=True, color=INK, align=PP_ALIGN.RIGHT)
        colors = {"готово": (GREEN_SOFT, GREEN), "проверить": (AMBER_SOFT, AMBER), "не заявка": (RED_SOFT, RED)}
        fill, color = colors[status]
        chip(slide, x + w - 1.55, row_y + 0.09, 1.15, status, fill=fill, color=color, size=9)


def rfq_scheme(slide, x, y, w, h):
    browser(slide, x, y, w, h, "127.0.0.1:8000/ui/rfq/…")
    top = y + 0.34
    text(slide, x + 0.25, top + 0.2, 5.2, 0.35, "Заявка · СтройМонтаж", size=16, bold=True, color=INK)
    chip(slide, x + 0.25, top + 0.66, 1.5, "готово к КП", fill=GREEN_SOFT, color=GREEN, size=9)
    text(slide, x + 0.25, top + 1.12, 5.2, 1.75,
         "Добрый день!\n\nПрошу рассчитать:\n1. Уголок 50×50 — 200 шт\n2. Кабель ВВГ 3×2.5 — 150 м\n3. Труба ППР 25 — 60 м\n4. Саморез 4.2×75 — 10 уп\n5. Цемент М500 — 40 меш",
         size=9, color=INK_SOFT, spacing=1.05)
    rect(slide, x + 0.25, top + 3.0, 5.2, 0.82, ACCENT_SOFT, radius=0.1)
    text(slide, x + 0.4, top + 3.1, 4.9, 0.65, "Сумма без НДС 58 660,00 ₽\nНДС 20% 11 732,00 ₽ · итого 70 392,00 ₽",
         size=10, bold=True, color=INK)
    text(slide, x + 5.8, top + 0.2, 2.2, 0.3, "Что понял сервис", size=12, bold=True, color=MUTED)
    lines = (
        ("Уголок 50×50", "200 шт", "KR-001", "в прайсе"),
        ("Кабель ВВГ 3×2.5", "150 м", "EL-001", "в прайсе"),
        ("Труба ППР 25", "60 м", "PP-025", "в прайсе"),
        ("Саморез 4.2×75", "10 уп", "SN-008", "в прайсе"),
        ("Цемент М500", "40 меш", "MT-007", "похоже"),
    )
    for index, (name, qty, sku, status) in enumerate(lines):
        row_y = top + 0.6 + index * 0.62
        rect(slide, x + 5.8, row_y, w - 6.1, 0.52, WHITE, line=LINE, radius=0.08)
        text(slide, x + 5.95, row_y + 0.08, 2.4, 0.32, name, size=10, bold=True, color=INK)
        text(slide, x + 8.3, row_y + 0.1, 0.9, 0.3, qty, size=9, color=MUTED)
        text(slide, x + 9.2, row_y + 0.1, 0.9, 0.3, sku, size=9, color=MUTED)
        fill, color = (GREEN_SOFT, GREEN) if status == "в прайсе" else (AMBER_SOFT, AMBER)
        chip(slide, x + w - 1.7, row_y + 0.09, 1.3, status, fill=fill, color=color, size=8)


def build(prs: Presentation) -> None:
    # 1. титул
    slide = new_slide(prs, INK)
    rect(slide, 0, 0, W, 0.08, GOLD, radius=0)
    text(slide, 0.9, 1.5, 8, 0.4, "ПОРТФОЛИО И ПРЕДЛОЖЕНИЕ", size=12, bold=True, color=GOLD)
    text(slide, 0.9, 2.0, 9.5, 1.2, "kpbot", size=76, bold=True, color=WHITE)
    text(slide, 0.9, 3.25, 9.8, 0.9, "письмо с заявкой → готовое коммерческое предложение",
         size=24, color=RGBColor(0xCB, 0xD5, 0xE1))
    text(slide, 0.9, 4.35, 9.5, 0.6, "Для оптовиков и производств: разбор почты, прайс, НДС, КП, 1С.",
         size=14, color=RGBColor(0x94, 0xA3, 0xB8))
    for index, (value, label) in enumerate((("253", "автотестов"), ("2,2 с", "на письмо"), ("1С", "OData на живой базе"), ("10 000", "позиций прайса"))):
        x = 0.9 + index * 3.0
        text(slide, x, 5.5, 2.8, 0.55, value, size=30, bold=True, color=WHITE)
        text(slide, x, 6.05, 2.8, 0.35, label, size=11, color=RGBColor(0x94, 0xA3, 0xB8))
    text(slide, 0.9, 6.8, 8, 0.3, "ноябрь 2026 · демо-стенд и исходники по запросу", size=10, color=MUTED)

    # 2. проблема
    slide = new_slide(prs)
    header(slide, "Проблема", "КП собирают вручную — и теряют на этом день",
           "Типичный процесс оптовика: письмо в почте, цены в Excel, ошибки в сумме.")
    cards = (
        ("15–30 минут", "на одно КП: найти прайс, сопоставить позиции, посчитать НДС, оформить"),
        ("Excel и память", "цены и артикулы переносятся руками; опечатка в цене = убыток"),
        ("Заявка ждёт", "пока менеджер освободится, клиент уже получил предложение от конкурента"),
    )
    for index, (value, detail) in enumerate(cards):
        x = 0.85 + index * 4.05
        rect(slide, x, 2.55, 3.75, 2.55, WHITE, line=LINE, radius=0.08)
        text(slide, x + 0.3, 2.8, 3.2, 0.5, value, size=24, bold=True, color=ACCENT)
        text(slide, x + 0.3, 3.45, 3.15, 1.4, detail, size=12, color=INK_SOFT, spacing=1.15)
    rect(slide, 0.85, 5.55, 11.6, 0.9, ACCENT_SOFT, radius=0.1)
    text(slide, 1.1, 5.72, 11.1, 0.6, "Считаем в деньгах: 20 КП в день × 20 минут = 6,5 часов работы менеджера в неделю.",
         size=14, bold=True, color=INK)
    footer(slide, 2)

    # 3. как работает
    slide = new_slide(prs)
    header(slide, "Решение", "Письмо превращается в КП за секунды",
           "Модель не назначает цены: она извлекает позиции, цену считает код по прайсу.")
    steps = (
        ("1", "Письмо", "IMAP, .eml,\nпапка под заявки"),
        ("2", "Позиции", "правила +\nLLM как страховка"),
        ("3", "Прайс", "матчинг\nс объяснимым score"),
        ("4", "Расчёт", "Decimal,\nНДС, доставка"),
        ("5", "КП", "HTML, DOCX,\nPDF, Excel"),
        ("6", "1С", "лист «Для 1С»,\nостатки"),
    )
    for index, (number, title, detail) in enumerate(steps):
        x = 0.85 + index * 2.0
        rect(slide, x, 2.6, 1.75, 2.3, WHITE, line=LINE, radius=0.1)
        text(slide, x + 0.22, 2.75, 0.5, 0.4, number, size=14, bold=True, color=GOLD)
        text(slide, x + 0.22, 3.15, 1.4, 0.4, title, size=15, bold=True, color=INK)
        text(slide, x + 0.22, 3.65, 1.4, 1.0, detail, size=10, color=MUTED, spacing=1.1)
        if index < 5:
            text(slide, x + 1.82, 3.55, 0.3, 0.4, "→", size=18, bold=True, color=ACCENT)
    rect(slide, 0.85, 5.35, 11.6, 1.1, WHITE, line=LINE, radius=0.08)
    text(slide, 1.1, 5.5, 11.1, 0.8,
         "Человек в контуре: спорные строки не попадают в расчёт, отправка — только после одобрения.\n"
         "Дубли писем не создают вторую заявку, сомнительное подсвечивается, а не угадывается.",
         size=12, color=INK_SOFT, spacing=1.15)
    footer(slide, 3)

    # 4. входящие
    slide = new_slide(prs)
    header(slide, "Интерфейс", "Входящие: очередь заявок, а не почтовый ящик")
    picture_or_scheme(slide, "02-inbox.png", 0.85, 2.15, 11.6, 4.45, inbox_scheme,
                      "Очередь заявок: кто прислал, сумма, статус")
    footer(slide, 4)

    # 5. карточка
    slide = new_slide(prs)
    header(slide, "Интерфейс", "Карточка: письмо рядом с разбором",
           "Видно, откуда взялась каждая цена и что сервис не понял.")
    picture_or_scheme(slide, "03-rfq.png", 0.85, 2.25, 11.6, 4.3, rfq_scheme,
                      "Письмо клиента и строки расчёта в одном экране")
    footer(slide, 5)

    # 6. документы
    slide = new_slide(prs)
    header(slide, "Документы", "Одни и те же данные — в нужном формате",
           "КП для клиента и файл для 1С собираются из одного расчёта.")
    formats = (
        ("HTML", "печать в PDF из браузера, письмо клиенту"),
        ("DOCX", "правки в Word, фирменный бланк"),
        ("PDF", "Chrome/WeasyPrint на сервере"),
        ("Excel", "расчёт, строки письма, лист «Для 1С»"),
        ("CSV", "1С: разделитель «;», десятичная запятая"),
    )
    for index, (name, detail) in enumerate(formats):
        x = 0.85 + (index % 3) * 3.95
        y = 2.5 + (index // 3) * 1.15
        rect(slide, x, y, 3.65, 0.95, WHITE, line=LINE, radius=0.08)
        text(slide, x + 0.25, y + 0.18, 0.9, 0.4, name, size=17, bold=True, color=ACCENT)
        text(slide, x + 1.2, y + 0.2, 2.3, 0.6, detail, size=10, color=INK_SOFT)
    table(slide, 0.85, 4.9, 7.4, [
        ("Код", "Наименование", "Кол-во", "Цена", "Сумма", "Склад", "НДС"),
        ("KR-001", "Уголок 50×50", "200", "34,50", "6 900,00", "Основной", "20%"),
        ("EL-001", "Кабель ВВГ 3×2,5", "150", "128,00", "19 200,00", "Основной", "20%"),
        ("PP-025", "Труба ППР 25", "60", "96,40", "5 784,00", "Основной", "20%"),
    ], [0.9, 2.1, 0.8, 0.9, 1.2, 1.1, 0.7], size=10, row_h=0.4, header_h=0.45)
    text(slide, 8.65, 4.9, 3.8, 1.9,
         "Лист «Для 1С» грузится стандартной обработкой\n«Загрузка данных из табличного документа».\n\n"
         "Проверка перед выгрузкой: колонки, числа, повтор артикула.",
         size=11, color=INK_SOFT, spacing=1.2)
    footer(slide, 6)

    # 7. 1С
    slide = new_slide(prs)
    header(slide, "1С", "Что проверено на живой базе, а что — нет",
           "1С:Бухгалтерия на Fresh, OData, пользователь с ограниченными правами. Проверка 26.11.2026.")
    rows = [
        ("Проверка", "Результат"),
        ("Чтение метаданных и справочников", "✅ 1067 сущностей, номенклатура, склады, организации"),
        ("Единицы измерения", "✅ «шт», «кг» — из классификатора, а не GUID"),
        ("Остатки", "⚠️ регистра «Товары на складах» в Бухгалтерии нет"),
        ("Создание номенклатуры", "✅ 201; изменение — 200; удаление — запрещено правами"),
        ("Счёт на оплату: шапка", "✅ 201 — организация, контрагент, валюта, склад"),
        ("Счёт на оплату: строки", "❌ Fresh отбивает табличную часть (500) — грузим файлом"),
        ("«Заказ клиента»", "❌ в Бухгалтерии такого документа нет — команда говорит прямо"),
    ]
    table(slide, 0.85, 2.3, 11.6, rows, [4.0, 7.6], size=11, row_h=0.44, header_h=0.45)
    rect(slide, 0.85, 6.15, 11.6, 0.65, ACCENT_SOFT, radius=0.08)
    text(slide, 1.1, 6.27, 11.1, 0.45,
         "Вывод: файл «Для 1С» — сегодня; прямая запись — HTTP-сервис (шаблон в deploy/1c) или база УТ/ERP.",
         size=12, bold=True, color=INK)
    footer(slide, 7)

    # 8. надёжность
    slide = new_slide(prs)
    header(slide, "Надёжность", "Приёмка: что уже проверено",
           "253 автотеста, изолированный прогон, ноль внешних вызовов.")
    columns = (
        ("Тесты", ("253 теста, 49 секунд", "боевой .env не читается", "сеть в тестах запрещена", "сквозной HTTP-сценарий")),
        ("Безопасность", ("параметризованный SQL", "CSRF, CSP, X-Frame-Options", "шифрование паролей ящиков", "ключи не попадают в логи")),
        ("Эксплуатация", ("бэкап + проверка восстановления", "сторож: healthz и журнал", "doctor.py перед демо", "smoke.py после деплоя")),
    )
    for index, (title, items) in enumerate(columns):
        x = 0.85 + index * 4.0
        rect(slide, x, 2.45, 3.7, 3.6, WHITE, line=LINE, radius=0.08)
        text(slide, x + 0.3, 2.65, 3.2, 0.4, title, size=15, bold=True, color=ACCENT)
        for row, item in enumerate(items):
            text(slide, x + 0.3, 3.2 + row * 0.62, 3.1, 0.5, "• " + item, size=11, color=INK_SOFT, spacing=1.1)
    text(slide, 0.85, 6.3, 11.6, 0.4, "Проверки повторяемы: scripts/acceptance.py собирает отчёт одной командой.",
         size=12, color=MUTED)
    footer(slide, 8)

    # 9. замеры
    slide = new_slide(prs)
    header(slide, "Замеры", "Скорость, которую видно на прайсе клиента")
    table(slide, 0.85, 2.3, 7.6, [
        ("Позиций в прайсе", "Индекс", "20 строк", "На строку"),
        ("100", "4,4 мс", "5,5 мс", "0,28 мс"),
        ("1 000", "46 мс", "45 мс", "2,25 мс"),
        ("10 000", "473 мс", "82 мс", "4,08 мс"),
    ], [2.6, 1.7, 1.7, 1.6], size=12, row_h=0.5, header_h=0.5)
    text(slide, 0.85, 4.65, 7.4, 0.6, "10 000 позиций — 245 строк в секунду на одном ядре.",
         size=14, bold=True, color=INK)
    for index, (value, label) in enumerate((("2,2 с", "разбор письма на сервере"), ("3 из 7", "КП без правок на демо"), ("100%", "recall на размеченных письмах"))):
        x = 8.7 + (index % 2) * 2.1
        y = 2.35 + (index // 2) * 1.5
        rect(slide, x, y, 1.95, 1.25, WHITE, line=LINE, radius=0.08)
        text(slide, x + 0.15, y + 0.18, 1.65, 0.45, value, size=18, bold=True, color=ACCENT)
        text(slide, x + 0.15, y + 0.7, 1.65, 0.45, label, size=9, color=MUTED, spacing=1.05)
    text(slide, 8.7, 5.35, 4.1, 1.4,
         "Замеры — scripts/bench.py.\nПисьмо и КП: демо-набор из 8 писем\nс опечатками и количествами словами.",
         size=10, color=MUTED, spacing=1.2)
    footer(slide, 9)

    # 10. экономика
    slide = new_slide(prs)
    header(slide, "Экономика", "Цена — меньше одного дня менеджера",
           "Ориентир: 20 КП в день по 15–30 минут.")
    rect(slide, 0.85, 2.4, 5.6, 3.6, WHITE, line=LINE, radius=0.08)
    text(slide, 1.15, 2.65, 5.0, 0.4, "Клиент экономит", size=14, bold=True, color=MUTED)
    text(slide, 1.15, 3.1, 5.0, 0.7, "15–30 тыс ₽/мес", size=28, bold=True, color=GREEN)
    text(slide, 1.15, 3.85, 5.0, 1.8, "1–2 часа в день рабочего времени менеджера.\nПлюс меньше ошибок в цене и быстрее ответ клиенту.",
         size=12, color=INK_SOFT, spacing=1.2)
    rect(slide, 6.85, 2.4, 5.6, 3.6, WHITE, line=LINE, radius=0.08)
    text(slide, 7.15, 2.65, 5.0, 0.4, "Стоимость", size=14, bold=True, color=MUTED)
    text(slide, 7.15, 3.1, 5.0, 0.7, "49 000 ₽ + 6 900 ₽/мес", size=28, bold=True, color=ACCENT)
    text(slide, 7.15, 3.85, 5.0, 1.8, "Внедрение: прайс, почта, обучение.\nПилот — месяц, с замером «до/после».",
         size=12, color=INK_SOFT, spacing=1.2)
    rect(slide, 0.85, 6.25, 11.6, 0.6, GREEN_SOFT, radius=0.08)
    text(slide, 1.1, 6.35, 11.1, 0.4, "Окупаемость: 2–3 месяца. Дальше — экономия и скорость ответа.",
         size=12, bold=True, color=GREEN)
    footer(slide, 10)

    # 11. внедрение
    slide = new_slide(prs)
    header(slide, "Внедрение", "Три шага, каждый проверяемый")
    steps = (
        ("День 1", "Стенд", "Демо-прайс, три письма, доступ по ссылке.\nСмотрим интерфейс и КП."),
        ("Неделя 1", "Данные клиента", "Прайс 100+ позиций, почта, реквизиты.\nПрогон на его письмах — отчёт."),
        ("Неделя 2", "Пилот", "20–30 реальных заявок.\nКритерий: доля строк без правок и время до КП."),
    )
    for index, (when, title, detail) in enumerate(steps):
        x = 0.85 + index * 4.0
        rect(slide, x, 2.4, 3.7, 3.2, WHITE, line=LINE, radius=0.08)
        text(slide, x + 0.3, 2.6, 3.1, 0.35, when.upper(), size=10, bold=True, color=GOLD)
        text(slide, x + 0.3, 3.0, 3.1, 0.5, title, size=20, bold=True, color=INK)
        text(slide, x + 0.3, 3.6, 3.1, 1.8, detail, size=11, color=INK_SOFT, spacing=1.15)
    rect(slide, 0.85, 5.9, 11.6, 0.85, WHITE, line=LINE, radius=0.08)
    text(slide, 1.1, 6.05, 11.1, 0.55, "Что нужно от клиента: прайс, доступ к ящику (или пересылка писем) и 20–30 примеров заявок.",
         size=12, color=INK_SOFT)
    footer(slide, 11)

    # 12. ограничения
    slide = new_slide(prs)
    header(slide, "Честно", "Что пока не готово и почему это важно",
           "Лучше услышать это до пилота, чем после.")
    rows = [
        ("Ограничение", "Что это значит", "Статус"),
        ("Сервер за пределами РФ", "нельзя продавать российским юрлицам (152-ФЗ)", "блокер"),
        ("Документы по ПДн не подписаны", "поручение, политика, уведомление РКН", "до клиента"),
        ("1С: табличная часть через OData", "счёт с позициями — только файлом", "ограничение базы"),
        ("Один разработчик", "bus factor; нужен runbook и второй человек", "риск"),
    ]
    table(slide, 0.85, 2.35, 11.6, rows, [3.4, 6.4, 1.8], size=11, row_h=0.62, header_h=0.45)
    text(slide, 0.85, 6.2, 11.6, 0.5, "Поэтому предложение — пилот, а не «купите коробку»: проверяем на данных клиента и фиксируем цифры.",
         size=12, bold=True, color=INK)
    footer(slide, 12)

    # 13. финал
    slide = new_slide(prs, INK)
    rect(slide, 0, 0, W, 0.08, GOLD, radius=0)
    text(slide, 0.9, 1.6, 8, 0.4, "СЛЕДУЮЩИЙ ШАГ", size=12, bold=True, color=GOLD)
    text(slide, 0.9, 2.1, 11.5, 1.0, "Покажем на вашей номенклатуре", size=40, bold=True, color=WHITE)
    text(slide, 0.9, 3.2, 10.5, 1.3,
         "Пришлите прайс и 20–30 писем — вернёмся с отчётом:\nсколько строк разобрано без человека, сколько времени экономит и что осталось спорным.",
         size=18, color=RGBColor(0xCB, 0xD5, 0xE1), spacing=1.2)
    text(slide, 0.9, 5.3, 6, 0.4, "kpbot · демо-стенд, исходники, тесты — по запросу", size=14, color=RGBColor(0x94, 0xA3, 0xB8))
    text(slide, 0.9, 6.5, 10, 0.35, "Материалы подготовлены на демо-данных; скриншоты и замеры — со стенда.", size=10, color=MUTED)


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать презентацию kpbot")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    build(prs)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    prs.save(target)

    shots = sorted(SHOTS.glob("*.png")) if SHOTS.exists() else []
    print(f"слайдов: {len(prs.slides)} · скриншотов найдено: {len(shots)}")
    print("файл:", target, target.stat().st_size, "байт")
    if not shots:
        print("подсказка: python scripts/capture_shots.py — затем пересоберите колоду")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Готовые письма-заявки для проверки качества на реальном IMAP.

Каждое письмо проверяет свой тип «грязи»: аккуратный список, одна строка через запятую,
опечатки и пропущенные единицы, позиции вне прайса, количества словами.
Отправляются в отдельную папку (по умолчанию kpbot-test), входящие не затрагиваются.
"""

from __future__ import annotations

LETTERS: tuple[dict[str, str], ...] = (
    {
        "sender": "Снабжение ООО «СтройМонтаж» <zakaz@stroymontazh.example>",
        "subject": "Заявка на материалы",
        "body": """Добрый день!

Прошу рассчитать:
1. Уголок 50х50 — 200 шт
2. Кабель ВВГ 3х2.5 — 150 м
3. Труба ППР 25 — 60 м
4. Саморез по дереву 4.2х75 — 10 уп
5. Цемент М500 — 40 мешков

Доставка на объект, оплата по счёту.
""",
    },
    {
        "sender": "Ирина <irina.p@remont-plus.example>",
        "subject": "Заявка",
        "body": """Нужно 5 листов гипсокартона 12.5, 10 шт профиль ПН-27/28,
2 мешка штукатурки гипсовой, 10 уп саморез по металлу 3.5х25, 1 шт унитаз-компакт.
Срок — до пятницы.
""",
    },
    {
        "sender": "Виктор <v.kuznetsov@master76.example>",
        "subject": "Заявка срочно!!",
        "body": """здравствуйте нужно срочно
- кабел ввгнг 3х1.5 – 300 м;
- автомат 16А – 4 шт;
- гафра 20 – 100 м;
- стяжка нейлоновая 200х4.8 – 3
- изолента
""",
    },
    {
        "sender": "Алексей <a.morozov@object21.example>",
        "subject": "Заявка на объект",
        "body": """Добрый день, посчитайте пожалуйста:
- подрозетник 68 мм — 20 шт
- уголок 40х40 — 50 шт
- монтаж кабель-канала — 30 м
- перчатки нитриловые L — 2 уп
""",
    },
    {
        "sender": "Мария <m.lebedeva@sklad-dvor.example>",
        "subject": "Заявка",
        "body": """Добрый день! Нужно полторы упаковки саморезов 4.2х75,
десяток уголков 50х50 и метров двадцать кабеля 3х2.5. Сколько будет стоить?
""",
    },
)


def build_raws(domain_email: str) -> list[bytes]:
    """Собирает письма в формате .eml с корректными заголовками."""
    import uuid
    from datetime import datetime, timezone
    from email.message import EmailMessage
    from email.utils import format_datetime, make_msgid

    raws: list[bytes] = []
    for index, letter in enumerate(LETTERS, start=1):
        msg = EmailMessage()
        msg["From"] = letter["sender"]
        msg["To"] = domain_email
        msg["Subject"] = letter["subject"]
        sent = datetime(2026, 11, 24, 9, index * 3, tzinfo=timezone.utc)
        msg["Date"] = format_datetime(sent)
        msg["Message-ID"] = f"<demo-{uuid.uuid4().hex}@kpbot.test>"
        msg.set_content(letter["body"], charset="utf-8")
        raws.append(msg.as_bytes())
    return raws
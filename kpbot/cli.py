"""CLI: наполнить демо-данными, прогнать сквозной сценарий, поднять сервер."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from .config import settings
from .domain import price_quote, status_for
from .logs import mask
from .mailbox import ImapConfig, ImapMailbox, MailError
from .mailparse import parse_eml
from .matching import match_lines
from .oauth import OAuthError, TokenStore, ensure_access_token, run_local_flow
from .pipeline import NotAnRfq, Pipeline
from .pricelist import default_price_items, write_csv, write_xlsx
from .store import Store

SAMPLES: tuple[dict, str] = (
    (
        {
            "name": "01_clean.eml",
            "sender": "Снабжение ООО «СтройМонтаж» <zakaz@stroymontazh.example>",
            "subject": "Заявка на материалы для отделки",
            "body": """Добрый день!

Прошу рассчитать стоимость следующих материалов:

1. Уголок 50х50 — 200 шт
2. Кабель ВВГ 3х2.5 — 150 м
3. Труба ППР 25 — 60 м
4. Саморез по дереву 4.2х75 — 10 уп
5. Цемент М500 — 40 мешков
6. Грунтовка глубокого проникновения — 5 канистр

Доставка нужна на объект: г. Москва, ул. Строителей, 15.
Оплата по счёту, отсрочка 5 дней.
""",
        }
    ),
    (
        {
            "name": "02_messy.eml",
            "sender": "Ирина <irina.p@remont-plus.example>",
            "subject": "нужен расчёт срочно",
            "body": """Здравствуйте, нужен расчёт.

- кабель ВВГнг 3х1.5 – 300 м;
- автомат 16А – 4 шт;
- подрозетник 68 мм – 20 шт;
- гофра 20 – 100 м;
- стяжка нейлоновая 200х4.8 – 3 уп

Подрозетник нужен срочно, остальное до конца недели.
Прошу выставить счёт.
""",
        }
    ),
    (
        {
            "name": "03_oneline.eml",
            "sender": "Виктор <v.kuznetsov@master76.example>",
            "subject": "Материалы на объект",
            "body": """Добрый день. Нужно 5 листов гипсокартона 12.5, 10 шт профиль ПН-27/28, 2 мешка штукатурки гипсовой, 10 уп саморез по металлу 3.5х25, 1 шт унитаз-компакт. Срок — до пятницы.
""",
        }
    ),
)


def _build_eml(sample: dict, index: int) -> bytes:
    msg = EmailMessage()
    msg["From"] = sample["sender"]
    msg["To"] = settings.company_email
    msg["Subject"] = sample["subject"]
    sent = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc) + timedelta(hours=index * 7)
    msg["Date"] = format_datetime(sent)
    msg["Message-ID"] = make_msgid(domain="example.com")
    msg.set_content(sample["body"], charset="utf-8")
    return msg.as_bytes()


def cmd_seed(args: argparse.Namespace) -> int:
    settings.ensure_dirs()
    items = default_price_items()
    xlsx = write_xlsx(settings.data_dir / "price_list.xlsx", items)
    csv_path = write_csv(settings.data_dir / "price_list.csv", items)
    print(f"прайс: {xlsx} ({len(items)} позиций)")
    print(f"прайс: {csv_path}")

    for index, sample in enumerate(SAMPLES, start=1):
        path = settings.samples_dir / sample["name"]
        path.write_bytes(_build_eml(sample, index))
        print(f"письмо: {path}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    settings.ensure_dirs()
    db_path = settings.db_path
    if args.fresh and db_path.exists():
        db_path.unlink()
        for old in settings.out_dir.glob("*.*"):
            old.unlink()

    if not (settings.samples_dir / SAMPLES[0]["name"]).exists():
        cmd_seed(args)

    store = Store(db_path)
    pipeline = Pipeline(store, settings)
    count = pipeline.ensure_price_list()
    print(f"прайс: {count} позиций\n")

    rows = []
    for sample in SAMPLES:
        raw = (settings.samples_dir / sample["name"]).read_bytes()
        result = pipeline.process_eml_bytes(raw, actor="cli")
        rows.append(result)
        print(f"--- {sample['name']}")
        print(f"    заявка {result.rfq_id[:8]} · статус {result.status} · "
              f"строк {result.lines} · в расчёте {result.matched} · итого {result.total} ₽")
        for item in result.excluded:
            print(f"    ! не в расчёте: {item}")
        print(f"    КП: {result.html_path}")
        if result.docx_path:
            print(f"    DOCX: {result.docx_path}")
        if result.pdf_path:
            print(f"    PDF: {result.pdf_path}")
        elif result.pdf_error:
            print(f"    PDF: недоступен ({result.pdf_error})")
        print()

    if rows:
        pipeline.approve(rows[0].rfq_id, actor="cli", comment="демо: одобрено автоматически для примера")
        print(f"пример одобрения: outbox -> {settings.out_dir / 'sent'}")
    print(f"\nитого заявок в базе: {store.stats()}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"UI:  http://127.0.0.1:{args.port}/ui   (пароль из KPBOT_UI_PASSWORD)")
    print(f"API: http://127.0.0.1:{args.port}/api/docs   (ключ из KPBOT_API_KEY)")
    uvicorn.run("kpbot.web:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_price(args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    path = Path(args.path) if args.path else settings.data_dir / "price_list.xlsx"
    if not path.exists():
        path = settings.data_dir / "price_list.csv"
    count = Pipeline(store, settings).reload_price_list(path)
    print(f"загружено {count} позиций из {path}")
    return 0


def _mailbox() -> ImapMailbox:
    """OAuth-токен приоритетнее пароля: у Gmail пароль приложения часто недоступен."""
    access_token = ""
    if settings.oauth_client_id and settings.oauth_client_secret:
        try:
            access_token = ensure_access_token(
                TokenStore(settings.oauth_token_path),
                settings.oauth_client_id,
                settings.oauth_client_secret,
            )
        except OAuthError as exc:
            print(f"OAuth: {exc}")

    return ImapMailbox(
        ImapConfig(
            host=settings.imap_host,
            port=settings.imap_port,
            user=settings.imap_user,
            password=settings.imap_password,
            access_token=access_token,
            folder=settings.imap_folder,
        )
    )


def cmd_oauth_login(args: argparse.Namespace) -> int:
    if not settings.oauth_client_id or not settings.oauth_client_secret:
        print("нужен OAuth-клиент: создайте «Desktop app» в Google Cloud, включите Gmail API")
        print("и заполните KPBOT_OAUTH_CLIENT_ID / KPBOT_OAUTH_CLIENT_SECRET в .env")
        return 2
    try:
        token = run_local_flow(
            settings.oauth_client_id,
            settings.oauth_client_secret,
            TokenStore(settings.oauth_token_path),
            port=args.port,
            open_browser=not args.no_browser,
        )
    except OAuthError as exc:
        print(f"не вышло: {exc}")
        return 2
    scope = "получен" if token.refresh_token else "НЕ получен"
    print(f"refresh-токен {scope} и сохранён в {settings.oauth_token_path}")
    return 0


def cmd_mail_status(args: argparse.Namespace) -> int:
    """Диагностика без сети: что настроено, чего не хватает, что делать дальше."""
    print(f"почтовый ящик: {settings.imap_user or '— не задан KPBOT_IMAP_USER'}")
    print(f"сервер:        {settings.imap_host}:{settings.imap_port} / {settings.imap_folder}")

    client_ok = bool(settings.oauth_client_id and settings.oauth_client_secret)
    id_hint = ""
    if settings.oauth_client_id:
        id_hint = " (похоже на верный)" if settings.oauth_client_id.endswith(".apps.googleusercontent.com") else \
                  " (не похоже: ожидается вид 1234-abc.apps.googleusercontent.com)"
    print(f"OAuth-клиент:  {'задан' if client_ok else 'НЕ задан'}{id_hint}")

    store = TokenStore(settings.oauth_token_path)
    token = store.load()
    if token.refresh_token:
        state = "просрочен, обновится сам" if token.expired else "активен"
        print(f"токен:         есть ({state}) — {settings.oauth_token_path}")
    else:
        print(f"токен:         нет — нужен `python -m kpbot.cli oauth-login`")

    print(f"пароль прил.:  {'задан' if settings.imap_password else 'нет'}")
    llm_state = "включена" if settings.llm_enabled else "выключена"
    print(f"LLM:           {llm_state} · модель {settings.llm_model or '—'} · ключ {mask(settings.llm_api_key)}")
    print(f"PDF:           {'включён' if settings.pdf_enabled else 'выключен'}")
    print(f"БД:            {settings.db_path} ({'есть' if settings.db_path.exists() else 'будет создана'})")

    if not client_ok and not settings.imap_password:
        print("\nДальше: создайте OAuth-клиент (Desktop app) в Google Cloud и заполните .env,")
        print("затем `python -m kpbot.cli oauth-login`. Подробности — README, раздел «Почта».")
        return 2
    if client_ok and not token.refresh_token:
        print("\nДальше: `python -m kpbot.cli oauth-login`")
        return 1
    print("\nВсё настроено: `python -m kpbot.cli mail-check`")
    return 0


def cmd_mail_check(args: argparse.Namespace) -> int:
    try:
        mailbox = _mailbox()
        info = mailbox.check()
    except MailError as exc:
        print(f"почта недоступна: {exc}")
        return 2
    print(f"подключение ок ({info['auth']}): {settings.imap_user} / {info['folder']} — "
          f"писем {info['total']}, непрочитанных {info['unseen']}")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()

    try:
        mailbox = _mailbox()
        info = mailbox.check()
        print(f"ящик: {info['folder']} · писем {info['total']} · непрочитанных {info['unseen']}")
        if args.dry_run:
            print("режим проверки: письма не забирались")
            return 0
        messages = mailbox.fetch_unseen(limit=args.limit)
    except MailError as exc:
        print(f"почта недоступна: {exc}")
        return 2

    if not messages:
        print("новых писем нет")
        return 0

    processed: list[str] = []
    skipped: list[dict[str, str]] = []
    for uid, raw in messages:
        try:
            result = pipeline.process_eml_bytes(raw, actor="imap")
        except NotAnRfq:
            subject, sender = _peek_subject(raw)
            skipped.append({"uid": uid, "sender": sender, "subject": subject, "reason": "ни одной позиции"})
            print(f"  uid {uid}: не заявка (ни одной позиции) — {subject[:60]}")
            continue
        except ValueError as exc:
            skipped.append({"uid": uid, "sender": "", "subject": "", "reason": str(exc)})
            print(f"  uid {uid}: пропущено — {exc}")
            continue
        processed.append(uid)
        print(f"  uid {uid}: заявка {result.rfq_id[:8]} · {result.status} · "
              f"строк {result.lines} · в расчёте {result.matched} · итого {result.total} ₽")

    if settings.imap_mark_seen and processed:
        mailbox.mark_seen(processed)
        print(f"помечено прочитанными: {len(processed)}")

    if skipped:
        _log_skipped(skipped)
        print(f"не заявки: {len(skipped)} — записаны в {settings.out_dir / 'skipped.jsonl'}")
        if settings.imap_mark_seen and settings.imap_mark_skipped:
            mailbox.mark_seen([item["uid"] for item in skipped])
            print("не заявки помечены прочитанными, чтобы не разбирать их повторно")
    return 0


def _peek_subject(raw: bytes) -> tuple[str, str]:
    try:
        parsed = parse_eml(raw)
    except ValueError:
        return "", ""
    return parsed.subject[:200], parsed.sender[:200]


def _log_skipped(items: list[dict[str, str]]) -> None:
    import json

    path = settings.out_dir / "skipped.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps({**item, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                ensure_ascii=False) + "\n")


def _telegram_config():
    from .notify import TelegramConfig

    return TelegramConfig(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        api_base=settings.telegram_api_base,
        ip_override=settings.telegram_ip_override,
    )


def cmd_telegram_info(args: argparse.Namespace) -> int:
    """Показывает бота и чаты, которые ему писали. С --save записывает chat_id в .env."""
    import re
    from pathlib import Path

    from .notify import NotifyError, bot_username, load_chat_id

    config = _telegram_config()
    try:
        username = bot_username(config)
        chats = load_chat_id(config)
    except NotifyError as exc:
        print(f"Telegram недоступен: {exc}")
        return 2

    print(f"бот: @{username}")
    if not chats:
        print("чатов нет: откройте бота в Telegram и нажмите «Start» (или отправьте любое сообщение)")
        return 1

    for chat_id, title in chats:
        print(f"  chat_id: {chat_id} ({title})")

    if args.save and len(chats) == 1:
        chat_id = chats[0][0]
        env_path = Path(".env")
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        pattern = re.compile(r"^KPBOT_TELEGRAM_CHAT_ID=.*$", re.M)
        line = f"KPBOT_TELEGRAM_CHAT_ID={chat_id}"
        text = pattern.sub(line, text) if pattern.search(text) else text.rstrip() + "\n" + line + "\n"
        env_path.write_text(text, encoding="utf-8")
        print(f"chat_id {chat_id} записан в .env")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    from .notify import NotifyError, send_telegram

    config = _telegram_config()
    if not config.chat_id:
        print("не задан KPBOT_TELEGRAM_CHAT_ID — выполните: python -m kpbot.cli telegram-info --save")
        return 2
    try:
        send_telegram(config, "kpbot: проверка уведомлений. Это тестовое сообщение.")
    except NotifyError as exc:
        print(f"не отправлено: {exc}")
        return 2
    print("тестовое сообщение отправлено")
    return 0


def _max_config():
    from .notify import MaxConfig

    return MaxConfig(
        token=settings.max_bot_token,
        user_id=settings.max_user_id,
        chat_id=settings.max_chat_id,
        api_base=settings.max_api_base,
    )


def cmd_max_info(args: argparse.Namespace) -> int:
    """Показывает бота MAX и тех, кто ему писал. С --save пишет user_id в .env."""
    import re
    from pathlib import Path

    from .notify import NotifyError, load_max_user_ids, max_bot_info

    config = _max_config()
    try:
        info = max_bot_info(config)
        users = load_max_user_ids(config)
    except NotifyError as exc:
        print(f"MAX недоступен: {exc}")
        return 2

    print(f"бот: {info.get('name') or info.get('first_name')} (@{info.get('username', '—')})")
    if not users:
        print("сообщений боту нет: напишите боту в MAX любое сообщение")
        return 1
    for user_id, name in users:
        print(f"  user_id: {user_id} ({name})")
    if args.save and len(users) == 1:
        user_id = users[0][0]
        env_path = Path(".env")
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        pattern = re.compile(r"^KPBOT_MAX_USER_ID=.*$", re.M)
        line = f"KPBOT_MAX_USER_ID={user_id}"
        text = pattern.sub(line, text) if pattern.search(text) else text.rstrip() + "\n" + line + "\n"
        env_path.write_text(text, encoding="utf-8")
        print(f"user_id {user_id} записан в .env")
    return 0


def cmd_max_test(args: argparse.Namespace) -> int:
    from .notify import NotifyError, send_max

    config = _max_config()
    if not config.addressed:
        print("не задан KPBOT_MAX_USER_ID — выполните: python -m kpbot.cli max-info --save")
        return 2
    try:
        send_max(config, "kpbot: проверка уведомлений в MAX. Это тестовое сообщение.")
    except NotifyError as exc:
        print(f"не отправлено: {exc}")
        return 2
    print("тестовое сообщение в MAX отправлено")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Замер качества на размеченном наборе писем."""
    from .evalkit import evaluate, format_report

    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()
    print(format_report(evaluate(pipeline), verbose=args.verbose))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Консистентная копия базы через sqlite backup API + ротация старых копий."""
    import contextlib
    import sqlite3

    source = settings.db_path
    if not source.exists():
        print(f"базы нет: {source}")
        return 2

    target_dir = settings.data_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"kpbot-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"

    with contextlib.closing(sqlite3.connect(str(source))) as src, contextlib.closing(
        sqlite3.connect(str(target))
    ) as dst:
        src.backup(dst)

    copies = sorted(target_dir.glob("kpbot-*.sqlite3"))
    removed = 0
    for old in copies[: max(0, len(copies) - args.keep)]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            # файл занят другим процессом — не повод падать, удалим в следующий раз
            continue

    size_kb = target.stat().st_size / 1024
    print(f"бэкап: {target} ({size_kb:.0f} КБ), удалено старых: {removed}, храним: {args.keep}")
    return 0


def cmd_mail_seed(args: argparse.Namespace) -> int:
    """Кладёт демо-заявки в отдельную папку ящика. Входящие не затрагиваются."""
    from .demo_letters import LETTERS, build_raws

    raws = build_raws(settings.imap_user)
    try:
        mailbox = _mailbox()
        mailbox.ensure_folder(args.folder)
        added = mailbox.append(args.folder, raws)
    except MailError as exc:
        print(f"почта недоступна: {exc}")
        return 2

    print(f"в папку {args.folder} добавлено {added} писем (входящие не тронуты)")
    for letter in LETTERS:
        print(f"  · {letter['subject']} — {letter['sender'][:40]}")
    print(f"\nдальше: KPBOT_IMAP_FOLDER='{args.folder}' python -m kpbot.cli poll --limit 5")
    return 0


def cmd_mail_preview(args: argparse.Namespace) -> int:
    """Показывает, что бот увидит в письмах. Ничего не пишет в БД и не меняет ящик."""
    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()
    index = pipeline.price_index()

    try:
        messages = _mailbox().fetch_recent(
            args.limit, unseen_only=args.unseen, readonly=True, subject_contains=args.search
        )
    except MailError as exc:
        print(f"почта недоступна: {exc}")
        return 2

    if not messages:
        print("писем не найдено")
        return 0

    print(f"просмотр {len(messages)} писем · ящик только для чтения · в базу ничего не пишется\n")
    for uid, raw in messages:
        try:
            parsed = parse_eml(raw)
        except ValueError as exc:
            print(f"· uid {uid}: пропущено — {exc}\n")
            continue

        lines, quote, is_request = pipeline.analyze(parsed.text)
        print(f"· uid {uid} · {(parsed.sender or '—')[:45]}")
        print(f"  тема: {(parsed.subject or '—')[:70]}")
        if not is_request:
            print("  не заявка: ни одной позиции из прайса\n")
            continue
        if not lines:
            print("  заявка без количества: разобрать не удалось, нужно человеку\n")
            continue

        print(f"  строк найдено: {len(lines)} · в расчёте: {len(quote.lines)} · "
              f"сумма: {quote.total} ₽ · статус: {status_for(quote).value}")
        for line in lines[:8]:
            quantity = f"{line.qty} {line.unit}" if line.qty is not None else "—"
            print(f"    - {line.raw_text[:44]!r} × {quantity} "
                  f"→ {line.status.value} ({line.match_score:.2f})")
        if len(lines) > 8:
            print(f"    … ещё {len(lines) - 8}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    from .logs import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser(prog="kpbot", description="заявка из письма → КП")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="создать демо-прайс и письма")
    seed.set_defaults(func=cmd_seed)

    demo = sub.add_parser("demo", help="прогнать сквозной сценарий на демо-письмах")
    demo.add_argument("--fresh", action="store_true", help="удалить базу и артефакты перед прогоном")
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="поднять веб-интерфейс и API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    price = sub.add_parser("price", help="перезагрузить прайс из файла")
    price.add_argument("path", nargs="?", default="")
    price.set_defaults(func=cmd_price)

    mail_check = sub.add_parser("mail-check", help="проверить доступ к почте (только чтение)")
    mail_check.set_defaults(func=cmd_mail_check)

    mail_status = sub.add_parser("mail-status", help="диагностика настроек почты без сети")
    mail_status.set_defaults(func=cmd_mail_status)

    oauth_login = sub.add_parser("oauth-login", help="получить OAuth-токен Gmail (XOAUTH2)")
    oauth_login.add_argument("--port", type=int, default=settings.oauth_redirect_port)
    oauth_login.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    oauth_login.set_defaults(func=cmd_oauth_login)

    poll = sub.add_parser("poll", help="забрать новые письма из почты и разобрать их")
    poll.add_argument("--limit", type=int, default=settings.imap_limit)
    poll.add_argument("--dry-run", action="store_true", help="только показать состояние ящика")
    poll.set_defaults(func=cmd_poll)

    preview = sub.add_parser("mail-preview", help="показать разбор писем без записи в базу")
    preview.add_argument("--limit", type=int, default=3)
    preview.add_argument("--unseen", action="store_true", help="только непрочитанные")
    preview.add_argument("--search", default="", help='фильтр по теме письма, например "Заявка"')
    preview.set_defaults(func=cmd_mail_preview)

    mail_seed = sub.add_parser("mail-seed", help="положить демо-заявки в отдельную папку ящика")
    mail_seed.add_argument("--folder", default="kpbot-test")
    mail_seed.set_defaults(func=cmd_mail_seed)

    backup = sub.add_parser("backup", help="сделать копию базы и удалить старые")
    backup.add_argument("--keep", type=int, default=7, help="сколько копий хранить")
    backup.set_defaults(func=cmd_backup)

    evaluate_cmd = sub.add_parser("eval", help="замерить качество разбора на размеченных письмах")
    evaluate_cmd.add_argument("--verbose", "-v", action="store_true")
    evaluate_cmd.set_defaults(func=cmd_eval)

    tg_info = sub.add_parser("telegram-info", help="показать бота и чаты, писавшие ему")
    tg_info.add_argument("--save", action="store_true", help="записать chat_id в .env")
    tg_info.set_defaults(func=cmd_telegram_info)

    tg_test = sub.add_parser("telegram-test", help="отправить тестовое уведомление")
    tg_test.set_defaults(func=cmd_telegram_test)

    max_info = sub.add_parser("max-info", help="показать бота MAX и тех, кто ему писал")
    max_info.add_argument("--save", action="store_true", help="записать user_id в .env")
    max_info.set_defaults(func=cmd_max_info)

    max_test = sub.add_parser("max-test", help="отправить тестовое уведомление в MAX")
    max_test.set_defaults(func=cmd_max_test)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
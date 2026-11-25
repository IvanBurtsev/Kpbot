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
from .secrets_box import SecretError

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


def _tenant_token_path(db_path: Path) -> Path:
    """Токен клиента лежит рядом с его базой: data/tenants/<id>.oauth.json."""
    return db_path.with_suffix(".oauth.json")


def _mailbox(overrides: dict[str, str] | None = None, token_path: Path | None = None) -> ImapMailbox:
    """Ящик клиента: его настройки из базы приоритетнее общих из .env.

    Пароль расшифровывается ключом из `.env`; если пароля нет — пробуем OAuth
    (у клиента может быть свой OAuth-клиент и свой файл токена).
    """
    from .secrets_box import decrypt

    mail = overrides or {}
    password = settings.imap_password
    if mail.get("mail_imap_password"):
        password = decrypt(mail["mail_imap_password"], settings.secrets_key)

    client_id = mail.get("oauth_client_id") or settings.oauth_client_id
    client_secret = mail.get("oauth_client_secret") or settings.oauth_client_secret
    if client_secret:
        client_secret = decrypt(client_secret, settings.secrets_key)

    access_token = ""
    if not password and client_id and client_secret:
        try:
            access_token = ensure_access_token(
                TokenStore(token_path or settings.oauth_token_path),
                client_id,
                client_secret,
            )
        except OAuthError as exc:
            print(f"OAuth: {exc}")

    return ImapMailbox(
        ImapConfig(
            host=mail.get("mail_imap_host") or settings.imap_host,
            port=int(mail.get("mail_imap_port") or settings.imap_port),
            user=mail.get("mail_imap_user") or settings.imap_user,
            password=password,
            access_token=access_token,
            folder=mail.get("mail_imap_folder") or settings.imap_folder,
        )
    )


def cmd_oauth_login(args: argparse.Namespace) -> int:
    """Получает refresh-токен: общий или отдельный для клиента (--tenant)."""
    from .secrets_box import decrypt
    from .tenancy import TenantRegistry

    tenant_id = getattr(args, "tenant", "") or ""
    client_id = settings.oauth_client_id
    client_secret = settings.oauth_client_secret
    token_path = settings.oauth_token_path
    label = "основная база"

    if tenant_id:
        registry = TenantRegistry(settings.tenants_db_path, settings.db_path)
        tenant = registry.get(tenant_id)
        if tenant is None:
            print(f"клиент {tenant_id} не найден в реестре")
            return 2
        db_path = Path(tenant["db_path"])
        prefs = Store(db_path).get_settings()
        client_id = prefs.get("oauth_client_id") or client_id
        if prefs.get("oauth_client_secret"):
            client_secret = decrypt(prefs["oauth_client_secret"], settings.secrets_key)
        token_path = _tenant_token_path(db_path)
        label = f"{tenant['name']} ({tenant['id']})"

    if not client_id or not client_secret:
        print("нужен OAuth-клиент: создайте «Desktop app» в Google Cloud, включите Gmail API")
        print("и заполните KPBOT_OAUTH_CLIENT_ID / KPBOT_OAUTH_CLIENT_SECRET в .env")
        print("или передайте клиенту через mail-setup --oauth-client-id/--oauth-client-secret")
        return 2
    try:
        token = run_local_flow(
            client_id,
            client_secret,
            TokenStore(token_path),
            port=args.port,
            open_browser=not args.no_browser,
        )
    except OAuthError as exc:
        print(f"не вышло: {exc}")
        return 2
    scope = "получен" if token.refresh_token else "НЕ получен"
    print(f"refresh-токен {scope} и сохранён в {token_path} ({label})")
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


def cmd_mail_setup(args: argparse.Namespace) -> int:
    """Сохраняет ящик в базе клиента: ключи mail_imap_* читает poll --all-tenants."""
    import getpass

    from .tenancy import TenantRegistry

    if args.tenant:
        registry = TenantRegistry(settings.tenants_db_path, settings.db_path)
        tenant = registry.get(args.tenant)
        if tenant is None:
            print(f"клиент {args.tenant} не найден в реестре")
            return 2
        db_path = Path(tenant["db_path"])
        label = f"{tenant['name']} ({tenant['id']})"
    else:
        db_path = settings.db_path
        label = "основная база"

    password = args.password or getpass.getpass(f"пароль приложения для {args.user}: ")
    from .secrets_box import encrypt

    values = {
        "mail_imap_host": args.host,
        "mail_imap_port": str(args.port),
        "mail_imap_user": args.user,
        "mail_imap_folder": args.folder,
    }
    if password:
        values["mail_imap_password"] = encrypt(password, settings.secrets_key)
    if args.oauth_client_id:
        values["oauth_client_id"] = args.oauth_client_id
    if args.oauth_client_secret:
        values["oauth_client_secret"] = encrypt(args.oauth_client_secret, settings.secrets_key)

    store = Store(db_path)
    store.set_settings(values)
    store.log_event("mail_setup", "cli", f"{args.user} @ {args.host}:{args.port}")

    print(f"ящик сохранён: {label} · {args.user} @ {args.host}:{args.port} / {args.folder}")
    print("пароль зашифрован ключом из .env; без KPBOT_SECRET_KEY/SESSION_SECRET он не читается")

    try:
        info = _mailbox(store.get_settings()).check()
    except MailError as exc:
        print(f"проверка не прошла: {exc}")
        return 1
    print(f"связь есть ({info['auth']}): писем {info['total']}, непрочитанных {info['unseen']}")
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
    """Опрашивает почту: одна база или все клиенты (--all-tenants)."""
    if getattr(args, "all_tenants", False):
        return _poll_all_tenants(args)
    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()
    try:
        mailbox = _mailbox()
    except MailError as exc:
        print(f"почта недоступна: {exc}")
        return 2
    return _poll_mailbox(pipeline, mailbox, args)


def _poll_all_tenants(args: argparse.Namespace) -> int:
    """Свой ящик у каждого клиента: настройки лежат в его базе (mail_imap_*)."""
    status = 0
    polled = 0
    for tenant_id, name, store in tenant_stores():
        overrides = store.get_settings()
        if not (overrides.get("mail_imap_user") or settings.imap_user):
            print(f"{name} ({tenant_id}): почта не настроена — пропуск")
            continue
        token_path = (
            settings.oauth_token_path
            if tenant_id == "default"
            else _tenant_token_path(Path(tenant["db_path"]))
        )
        try:
            mailbox = _mailbox(overrides, token_path)
        except (MailError, SecretError) as exc:
            print(f"{name}: почта недоступна: {exc}")
            status = 2
            continue
        pipeline = Pipeline(store, settings)
        if store.price_count() == 0:
            if tenant_id == "default":
                pipeline.ensure_price_list()
            else:
                print(f"{name}: прайса нет — строки уйдут человеку на проверку")
        print(f"\n== {name} ({tenant_id})")
        status = max(status, _poll_mailbox(pipeline, mailbox, args))
        polled += 1
    if not polled:
        print("нечего опрашивать: ни у одного клиента не настроена почта")
    return status


def _poll_mailbox(pipeline: Pipeline, mailbox: ImapMailbox, args: argparse.Namespace) -> int:
    try:
        info = mailbox.check()
        print(f"ящик: {info['folder']} · писем {info['total']} · непрочитанных {info['unseen']}")
        if args.dry_run:
            print("режим проверки: письма не забирались")
            return 0
        messages = mailbox.fetch_unseen(limit=args.limit)
        if not messages:
            # фильтр почты мог пометить письма прочитанными — смотрим последние
            # в папке и опираемся на дедупликацию по message_id
            messages = mailbox.fetch_recent(args.limit, unseen_only=False, readonly=False)
            if messages:
                print("непрочитанных нет — проверяю последние письма в папке")
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


def cmd_export_1c(args: argparse.Namespace) -> int:
    """Выгружает заявку в файл для 1С и сразу проверяет его формат."""
    from .export import build_rfq_xlsx, check_1c_sheet

    store = Store(settings.db_path)
    rfq = store.get_rfq(args.rfq_id) if args.rfq_id else None
    if rfq is None:
        rows = store.list_rfqs(1)
        rfq = store.get_rfq(rows[0]["id"]) if rows else None
    if rfq is None:
        print("заявок нет — выгружать нечего")
        return 2

    if args.csv:
        from .export import build_rfq_csv

        files = build_rfq_csv(
            rfq, store.get_quote(rfq["id"]), settings.out_dir, stock=store.stock_map()
        )
        for path in files:
            print(f"файл: {path} ({path.stat().st_size} байт)")
        print("CSV: разделитель «;», десятичная запятая, кодировка UTF-8 с BOM")
        return 0

    content = build_rfq_xlsx(rfq, store.get_quote(rfq["id"]), stock=store.stock_map())
    target = Path(args.out) if args.out else settings.out_dir / f"1c-{rfq['id'][:8]}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    problems = check_1c_sheet(content)
    print(f"файл: {target} ({len(content)} байт) · заявка {rfq['id'][:8]}")
    if problems:
        print("проблемы:")
        for problem in problems:
            print("  -", problem)
        return 1
    print("формат для 1С в порядке: колонки, типы и суммы на месте")
    return 0


def cmd_send_kp(args: argparse.Namespace) -> int:
    """Отправляет готовое КП клиенту тем же путём, что и кнопка в интерфейсе."""
    from .mailer import MailError, Mailer, build_kp_message, load_attachments, reply_subject
    from .pipeline import build_reply_text_from_store

    store = Store(settings.db_path)
    rfq = store.get_rfq(args.rfq_id) if args.rfq_id else None
    if rfq is None:
        print("заявка не найдена")
        return 2
    quote = store.get_quote(rfq["id"])
    if quote is None:
        print("КП ещё не рассчитано")
        return 2

    to = args.to or str(rfq.get("sender", ""))
    if "@" not in to:
        print("нужен корректный адрес получателя")
        return 2

    mailer = Mailer(settings)
    attachments = load_attachments(settings.out_dir, rfq["id"], short=rfq["id"][:8])
    message = build_kp_message(
        sender=to,
        from_addr=mailer.from_addr or settings.smtp_user or "no-reply@localhost",
        subject=reply_subject(str(rfq.get("subject", ""))),
        body=args.body or build_reply_text_from_store(rfq, quote, rfq["id"]),
        attachments=attachments,
        reply_to=settings.smtp_user or "",
    )
    try:
        mailer.send(message, to=to)
    except MailError as exc:
        store.log(rfq["id"], "send_failed", "cli", f"{to}: {exc}"[:300])
        print(f"не отправлено: {exc}")
        return 1
    store.log(rfq["id"], "sent", "cli", f"кому {to}, вложений {len(attachments)}")
    print(f"отправлено: {to} · вложений {len(attachments)}")
    return 0


def cmd_smtp_check(args: argparse.Namespace) -> int:
    """Проверяет почтовые настройки, ничего не отправляя."""
    from .mailer import MailError, check_connection

    try:
        print(check_connection(settings))
    except MailError as exc:
        print(f"не настроено: {exc}")
        return 1
    return 0


def cmd_demo_stand(args: argparse.Namespace) -> int:
    """Демо-стенд на номенклатуре клиента: прайс из 1С + сгенерированные заявки."""
    from .demo import build_letters, sender_address
    from .pricelist import load_price_items

    settings.ensure_dirs()
    store = Store(settings.db_path)

    if args.price:
        path = Path(args.price)
        items = load_price_items(path)
        count = store.replace_price_items(items)
        print(f"прайс: {count} позиций из {path.name}")
    else:
        count = store.price_count()
        if not count:
            print("прайса нет: укажите --price файл из 1С или запустите seed")
            return 2
        print(f"прайс: {count} позиций (уже загружен)")
        items = store.price_items()

    if args.reset:
        removed = store.purge_demo_rfqs()
        print(f"удалено старых демо-заявок: {removed}")

    pipeline = Pipeline(store, settings)
    letters = build_letters(items, count=args.letters, seed=args.seed)
    print(f"заявок: {len(letters)}\n")
    total = 0.0
    for letter in letters:
        result = pipeline.process_text(letter.body, sender=letter.sender, subject=letter.subject)
        total += float(result.total)
        print(f"--- {letter.subject} · {sender_address(letter.sender)}")
        print(f"    {result.rfq_id[:8]} · {result.status} · строк {result.lines} · "
              f"в расчёте {result.matched} · итого {result.total} ₽")
        if result.excluded:
            print(f"    ! вне расчёта: {', '.join(result.excluded)}")
    print(f"\nитого по стенду: {total:.2f} ₽, заявок {len(letters)}")
    if settings.public_base_url:
        print(f"смотреть: {settings.public_base_url.rstrip('/')}/ui")
    return 0


def cmd_smtp_setup(args: argparse.Namespace) -> int:
    """Прописывает SMTP в .env, пароль спрашивает скрыто и никуда не печатает."""
    import getpass
    import re

    env_path = Path(args.env) if args.env else Path(".env")
    if not env_path.exists():
        print(f"нет файла {env_path}")
        return 2

    password = getpass.getpass("Пароль почты (не отображается): ")
    if not password:
        print("пароль не введён")
        return 2

    values = {
        "KPBOT_SMTP_ENABLED": "true",
        "KPBOT_SMTP_HOST": args.host,
        "KPBOT_SMTP_PORT": str(args.port),
        "KPBOT_SMTP_SECURITY": args.security,
        "KPBOT_SMTP_USER": args.user,
        "KPBOT_SMTP_PASSWORD": password,
        "KPBOT_SMTP_FROM": args.sender or args.user,
    }
    text = env_path.read_text(encoding="utf-8")
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip() + "\n" + line + "\n"
    env_path.write_text(text, encoding="utf-8")

    print(f"записано в {env_path}: {args.user} через {args.host}:{args.port} ({args.security})")
    print("пароль в вывод не попал; перезапустите сервис и выполните smtp-check")
    return 0


def _retry_one(store: Store, mailer, limit: int) -> tuple[int, int]:
    """Повтор отправки в одной базе: возвращает (отправлено, не удалось)."""
    from .mailer import MailError, build_kp_message, load_attachments, reply_subject

    sent = failed = 0
    for item in store.pending_messages(limit):
        rfq = store.get_rfq(item["rfq_id"])
        if rfq is None:
            store.mark_attempt_failed(item["id"], "заявка удалена")
            failed += 1
            continue
        attachments = load_attachments(settings.out_dir, item["rfq_id"], short=item["rfq_id"][:8])
        message = build_kp_message(
            sender=item["to_addr"],
            from_addr=mailer.from_addr or settings.smtp_user or "no-reply@localhost",
            subject=item["subject"] or reply_subject(str(rfq.get("subject", ""))),
            body=item["body"],
            attachments=attachments,
            reply_to=settings.smtp_user or "",
        )
        try:
            mailer.send(message, to=item["to_addr"])
        except MailError as exc:
            store.mark_attempt_failed(item["id"], str(exc))
            store.log(item["rfq_id"], "send_retry_failed", "cli", f"{item['to_addr']}: {exc}"[:300])
            failed += 1
            continue
        store.mark_sent(item["id"])
        store.log(item["rfq_id"], "sent", "cli:retry", f"кому {item['to_addr']}, вложений {len(attachments)}")
        sent += 1
    return sent, failed


def cmd_retry_outbox(args: argparse.Namespace) -> int:
    """Повторяет отправку писем, которые не ушли с первого раза."""
    from .mailer import MailError, Mailer, build_kp_message, load_attachments, reply_subject

    mailer = Mailer(settings)
    if getattr(args, "all", False):
        total_sent = total_failed = 0
        for tenant_id, name, tenant_store in tenant_stores():
            sent, failed = _retry_one(tenant_store, mailer, args.limit)
            stats = tenant_store.outbox_stats()
            print(f"  {name}: отправлено {sent}, не удалось {failed}, в очереди {stats['pending']}")
            total_sent += sent
            total_failed += failed
        print(f"итог по клиентам: отправлено {total_sent}, не удалось {total_failed}")
        return 0 if total_failed == 0 else 1

    store = Store(settings.db_path)
    pending = store.pending_messages(args.limit)
    if not pending:
        print("очередь пуста")
        return 0

    sent = failed = 0
    for item in pending:
        rfq = store.get_rfq(item["rfq_id"])
        if rfq is None:
            store.mark_attempt_failed(item["id"], "заявка удалена")
            failed += 1
            continue
        attachments = load_attachments(settings.out_dir, item["rfq_id"], short=item["rfq_id"][:8])
        message = build_kp_message(
            sender=item["to_addr"],
            from_addr=mailer.from_addr or settings.smtp_user or "no-reply@localhost",
            subject=item["subject"] or reply_subject(str(rfq.get("subject", ""))),
            body=item["body"],
            attachments=attachments,
            reply_to=settings.smtp_user or "",
        )
        try:
            mailer.send(message, to=item["to_addr"])
        except MailError as exc:
            store.mark_attempt_failed(item["id"], str(exc))
            store.log(item["rfq_id"], "send_retry_failed", "cli", f"{item['to_addr']}: {exc}"[:300])
            print(f"  не ушло: {item['to_addr']} — {exc}")
            failed += 1
            continue
        store.mark_sent(item["id"])
        store.log(item["rfq_id"], "sent", "cli:retry", f"кому {item['to_addr']}, вложений {len(attachments)}")
        print(f"  отправлено: {item['to_addr']}")
        sent += 1

    stats = store.outbox_stats()
    print(f"итог: отправлено {sent}, не удалось {failed}, в очереди осталось {stats['pending']}")
    return 0 if failed == 0 else 1


def cmd_alert(args: argparse.Namespace) -> int:
    """Отправляет служебное сообщение в подключённые каналы (сторож, cron, деплой)."""
    from .notify import MaxConfig, NotifyError, TelegramConfig, send_max, send_telegram

    text = (args.text or "").strip()[:900] or "kpbot: служебное сообщение"
    text = "kpbot\n" + text
    sent = 0
    if settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            send_telegram(
                TelegramConfig(
                    token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    api_base=settings.telegram_api_base,
                    ip_override=settings.telegram_ip_override,
                ),
                text,
            )
            sent += 1
        except NotifyError as exc:
            print(f"telegram: {exc}")
    if settings.max_enabled and settings.max_bot_token and (settings.max_user_id or settings.max_chat_id):
        try:
            send_max(
                MaxConfig(
                    token=settings.max_bot_token,
                    user_id=settings.max_user_id,
                    chat_id=settings.max_chat_id,
                    api_base=settings.max_api_base,
                ),
                text,
            )
            sent += 1
        except NotifyError as exc:
            print(f"max: {exc}")
    print(f"отправлено каналов: {sent}")
    return 0 if sent else 1


def cmd_user_add(args: argparse.Namespace) -> int:
    """Добавить менеджера или администратора. Пароль спрашиваем скрыто."""
    import getpass

    store = Store(settings.db_path)
    password = args.password or getpass.getpass("Пароль (мин. 8 символов): ")
    if password != (args.password or getpass.getpass("Повторите пароль: ")) and not args.password:
        print("пароли не совпадают")
        return 2
    try:
        user = store.create_user(email=args.email, password=password, role=args.role, name=args.name or "")
    except (ValueError, Exception) as exc:  # noqa: BLE001 - показываем причину человеку
        print(f"не создан: {exc}")
        return 2
    print(f"создан: {user.email} · {user.role} · {user.id[:8]}")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    rows = store.users()
    if not rows:
        print("пользователей нет")
        return 0
    for user in rows:
        print(f"  {user.email:28} {user.role:8} {user.name or '—'}")
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    """Сколько токенов и рублей съел разбор писем, и за сколько он их разбирает."""
    store = Store(settings.db_path)
    totals = store.llm_totals()
    timing = store.timing_stats()

    if timing["median_seconds"] is not None:
        print(f"разбор письма: медиана {timing['median_seconds']:.1f} с, "
              f"максимум {timing['max_seconds']:.1f} с (заявок: {timing['count']})")
        if timing.get("median_wait_seconds") is not None:
            print(f"ожидание в очереди: медиана {timing['median_wait_seconds']:.0f} с "
                  f"— уменьшается частотой опроса почты")

    print(f"модель:        {settings.llm_model or '—'}")
    print(f"вызовов LLM:   {totals['calls']} (заявок: {totals['rfqs']})")
    print(f"токены:        {totals['prompt_tokens']} на вход, {totals['completion_tokens']} на ответ")
    if totals["calls"]:
        per_call = (totals["prompt_tokens"] + totals["completion_tokens"]) / totals["calls"]
        print(f"в среднем:     {per_call:.0f} токенов на письмо")

    prices_set = settings.llm_price_input_rub_per_1m or settings.llm_price_output_rub_per_1m
    if totals["cost_rub"] or prices_set:
        if prices_set and not totals["cost_rub"]:
            print(f"тариф:         {settings.llm_price_input_rub_per_1m} ₽/1М вход, "
                  f"{settings.llm_price_output_rub_per_1m} ₽/1М ответ")
        print(f"расход:        {totals['cost_rub']:.4f} ₽ всего · {totals['today_cost_rub']:.4f} ₽ сегодня")
        if totals["rfqs"]:
            print(f"на заявку:     {totals['cost_rub'] / totals['rfqs']:.4f} ₽")
        if totals.get("balance_rub") is not None:
            print(f"баланс:        {totals['balance_rub']:.2f} ₽ (последний известный)")
    else:
        print("расход:        провайдер не вернул стоимость и тариф не задан")
        print("               заполните KPBOT_LLM_PRICE_INPUT_RUB_PER_1M / _OUTPUT_RUB_PER_1M")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Замер качества на размеченном наборе писем."""
    from .evalkit import evaluate, format_report

    store = Store(settings.db_path)
    pipeline = Pipeline(store, settings)
    pipeline.ensure_price_list()
    print(format_report(evaluate(pipeline), verbose=args.verbose))
    return 0


def _backup_one(source: Path, keep: int, suffix: str = "") -> str:
    """Копия одной базы с ротацией. Возвращает путь и размер для отчёта."""
    import contextlib
    import sqlite3

    if not source.exists():
        return f"нет базы {source}"
    target_dir = settings.data_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    name_part = f"kpbot-{suffix}-" if suffix else "kpbot-"
    target = target_dir / f"{name_part}{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"
    with contextlib.closing(sqlite3.connect(str(source))) as src, contextlib.closing(
        sqlite3.connect(str(target))
    ) as dst:
        src.backup(dst)

    copies = sorted(target_dir.glob(f"{name_part}*.sqlite3"))
    removed = 0
    for old in copies[: max(0, len(copies) - max(1, keep))]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    size_kb = target.stat().st_size // 1024
    return f"{target.name} ({size_kb} КБ), удалено старых: {removed}"


def tenant_stores():
    """Пары (код, название, Store) по всем активным клиентам.

    Если реестра нет — работает одна основная база, как раньше.
    """
    from .tenancy import TenantRegistry

    registry = TenantRegistry(settings.tenants_db_path, settings.db_path)
    tenants = registry.list()
    if not tenants:
        yield "default", "Основной", Store(settings.db_path)
        return
    for tenant in tenants:
        if tenant["status"] != "active":
            continue
        path = Path(tenant["db_path"])
        if path.exists():
            yield tenant["id"], tenant["name"], Store(path)


def _one_c_client():
    from .one_c import OneCClient

    if not settings.one_c_base_url:
        raise SystemExit("не задан KPBOT_ONE_C_BASE_URL — адрес базы 1С")
    return OneCClient(
        settings.one_c_base_url,
        settings.one_c_user,
        settings.one_c_password,
        settings.one_c_timeout_seconds,
    )


def cmd_one_c_check(args: argparse.Namespace) -> int:
    """Проверяет доступ к 1С по OData."""
    from .one_c import OneCError

    try:
        print(_one_c_client().check())
    except (OneCError, SystemExit) as exc:
        print(f"1С: {exc}")
        return 1
    return 0


def cmd_one_c_stock(args: argparse.Namespace) -> int:
    """Забирает остатки из 1С и кладёт в базу: дальше они видны в заявке.

    Источник: регистр остатков, а если он не опубликован — приход минус расход
    по документам (--source documents).
    """
    from .one_c import OneCError

    client = _one_c_client()
    try:
        if args.source == "register":
            rows = client.stock(limit=args.limit)
        elif args.source == "documents":
            rows = client.stock_from_documents(limit=args.limit)
        else:
            rows = client.stock_auto(limit=args.limit)
    except (OneCError, SystemExit) as exc:
        print(f"1С: {exc}")
        return 1
    store = Store(settings.db_path)
    count = store.replace_stock(rows)
    zero = sum(1 for _, qty, _, _ in rows if qty in ("0", "0.0", "0.00"))
    print(f"остатков из 1С: {count} | в нуле: {zero} | источник: {args.source}")
    print("кэш обновлён: страницы заявок читают остатки из базы, 1С повторно не опрашивается")
    return 0


def cmd_one_c_order(args: argparse.Namespace) -> int:
    """Создаёт «Заказ клиента» в 1С по подтверждённому расчёту заявки."""
    from .one_c import OneCError

    store = Store(settings.db_path)
    rfq = store.get_rfq(args.rfq_id) if args.rfq_id else None
    if rfq is None:
        rows = store.list_rfqs(1)
        rfq = store.get_rfq(rows[0]["id"]) if rows else None
    if rfq is None:
        print("заявок нет — создавать заказ не из чего")
        return 2

    quote = store.get_quote(rfq["id"])
    if not quote or not quote.get("lines"):
        print("в заявке нет расчёта: сначала подтвердите строки в интерфейсе")
        return 2

    lines = [(line["sku"], line["qty"], line["price"]) for line in quote["lines"]]
    try:
        result = _one_c_client().create_order(lines, counterparty_key=args.counterparty)
    except (OneCError, SystemExit) as exc:
        print(f"1С: {exc}")
        return 1
    store.log(rfq["id"], "order_1c", "cli", str(result)[:200])
    print(f"заказ создан в 1С: {result} · заявка {rfq['id'][:8]}")
    print("проверьте документ в 1С: реквизиты, которые сервис не знает, заполняет сама 1С")
    return 0


def cmd_one_c_invoice(args: argparse.Namespace) -> int:
    """Создаёт «Счёт на оплату покупателю» по подтверждённому расчёту заявки."""
    from .one_c import OneCError

    store = Store(settings.db_path)
    rfq = store.get_rfq(args.rfq_id) if args.rfq_id else None
    if rfq is None:
        rows = store.list_rfqs(1)
        rfq = store.get_rfq(rows[0]["id"]) if rows else None
    if rfq is None:
        print("заявок нет — создавать счёт не из чего")
        return 2

    quote = store.get_quote(rfq["id"])
    if not quote or not quote.get("lines"):
        print("в заявке нет расчёта: сначала подтвердите строки в интерфейсе")
        return 2
    if not args.counterparty:
        print("нужен --counterparty (Ref_Key контрагента из 1С)")
        return 2

    lines = [(line["sku"], line["qty"], line["price"]) for line in quote["lines"]]
    try:
        result = _one_c_client().create_invoice(
            counterparty=args.counterparty, lines=lines
        )
    except (OneCError, SystemExit) as exc:
        print(f"1С: {exc}")
        return 1
    store.log(rfq["id"], "invoice_1c", "cli", str(result)[:200])
    print(f"счёт создан в 1С: {result} · заявка {rfq['id'][:8]}")
    print("проверьте документ в 1С: часть реквизитов заполняет сама 1С")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Консистентная копия базы через sqlite backup API + ротация старых копий."""
    import contextlib
    import sqlite3

    if getattr(args, "all", False):
        total = 0
        for tenant_id, name, tenant_store in tenant_stores():
            result = _backup_one(tenant_store.db_path, args.keep, suffix=tenant_id)
            print(f"  {name} ({tenant_id}): {result}")
            total += 1
        print(f"клиентов в бэкапе: {total}")
        return 0

    source = settings.db_path
    if not source.exists():
        print(f"базы нет: {source}")
        return 2
    print(f"бэкап: {_backup_one(source, args.keep)}")
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
    from .logs import register_settings_secrets, setup_logging

    # Консоль Windows не умеет часть символов (→, ≥): не падаем на --help.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    setup_logging()
    register_settings_secrets(settings)
    parser = argparse.ArgumentParser(prog="kpbot", description="заявка из письма → КП")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="создать демо-прайс и письма")
    seed.set_defaults(func=cmd_seed)

    demo_stand = sub.add_parser("demo-stand", help="демо-стенд на номенклатуре клиента")
    demo_stand.add_argument("--price", default="", help="файл выгрузки из 1С (xlsx/csv)")
    demo_stand.add_argument("--letters", type=int, default=6)
    demo_stand.add_argument("--seed", type=int, default=42)
    demo_stand.add_argument("--reset", action="store_true", help="удалить прежние демо-заявки")
    demo_stand.set_defaults(func=cmd_demo_stand)

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
    oauth_login.add_argument("--tenant", default="", help="код клиента: свой OAuth-клиент и свой токен")
    oauth_login.set_defaults(func=cmd_oauth_login)

    poll = sub.add_parser("poll", help="забрать новые письма из почты и разобрать их")
    poll.add_argument("--limit", type=int, default=settings.imap_limit)
    poll.add_argument("--dry-run", action="store_true", help="только показать состояние ящика")
    poll.add_argument(
        "--all-tenants",
        action="store_true",
        help="обойти все активные базы клиентов: у каждого свой ящик из его настроек",
    )
    poll.set_defaults(func=cmd_poll)

    mail_setup = sub.add_parser("mail-setup", help="сохранить ящик клиента (mail_imap_*) в его базе")
    mail_setup.add_argument("--tenant", default="", help="код клиента; без него — основная база")
    mail_setup.add_argument("--host", required=True)
    mail_setup.add_argument("--port", type=int, default=993)
    mail_setup.add_argument("--user", required=True)
    mail_setup.add_argument("--folder", default="INBOX")
    mail_setup.add_argument("--password", default="", help="без флага спросим скрыто")
    mail_setup.add_argument("--oauth-client-id", default="", help="OAuth-клиент клиента (Desktop app)")
    mail_setup.add_argument("--oauth-client-secret", default="", help="секрет клиента; хранится зашифрованным")
    mail_setup.set_defaults(func=cmd_mail_setup)

    preview = sub.add_parser("mail-preview", help="показать разбор писем без записи в базу")
    preview.add_argument("--limit", type=int, default=3)
    preview.add_argument("--unseen", action="store_true", help="только непрочитанные")
    preview.add_argument("--search", default="", help='фильтр по теме письма, например "Заявка"')
    preview.set_defaults(func=cmd_mail_preview)

    mail_seed = sub.add_parser("mail-seed", help="положить демо-заявки в отдельную папку ящика")
    mail_seed.add_argument("--folder", default="kpbot-test")
    mail_seed.set_defaults(func=cmd_mail_seed)

    one_c_check = sub.add_parser("1c-check", help="проверить доступ к 1С по OData")
    one_c_check.set_defaults(func=cmd_one_c_check)

    one_c_stock = sub.add_parser("1c-stock", help="забрать остатки из 1С в базу")
    one_c_stock.add_argument("--limit", type=int, default=1000)
    one_c_stock.add_argument(
        "--source",
        choices=("auto", "register", "documents"),
        default="auto",
        help="auto — регистр, при отказе документы (по умолчанию); register — только регистр; "
             "documents — приход минус расход по документам",
    )
    one_c_stock.set_defaults(func=cmd_one_c_stock)

    one_c_order = sub.add_parser("1c-order", help="создать «Заказ клиента» в 1С по расчёту заявки")
    one_c_order.add_argument("--rfq", dest="rfq_id", default="", help="id заявки; по умолчанию последняя")
    one_c_order.add_argument("--counterparty", default="", help="Ref_Key контрагента (необязательно)")
    one_c_order.set_defaults(func=cmd_one_c_order)

    one_c_invoice = sub.add_parser("1c-invoice", help="создать «Счёт на оплату» в 1С по расчёту")
    one_c_invoice.add_argument("--rfq", dest="rfq_id", default="", help="id заявки; по умолчанию последняя")
    one_c_invoice.add_argument("--counterparty", default="", help="Ref_Key контрагента из 1С")
    one_c_invoice.set_defaults(func=cmd_one_c_invoice)

    backup = sub.add_parser("backup", help="сделать копию базы и удалить старые")
    backup.add_argument("--keep", type=int, default=7, help="сколько копий хранить")
    backup.add_argument("--all", action="store_true", help="все клиенты, не только основная база")
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

    costs = sub.add_parser("costs", help="расход токенов и рублей на разбор писем")
    costs.set_defaults(func=cmd_costs)

    user_add = sub.add_parser("user-add", help="добавить пользователя (менеджера или админа)")
    user_add.add_argument("--email", required=True)
    user_add.add_argument("--role", default="manager", choices=("manager", "admin"))
    user_add.add_argument("--name", default="")
    user_add.add_argument("--password", default="", help="необязательно: иначе спросим скрыто")
    user_add.set_defaults(func=cmd_user_add)

    retry = sub.add_parser("retry-outbox", help="повторить отправку писем из очереди")
    retry.add_argument("--limit", type=int, default=20)
    retry.add_argument("--all", action="store_true", help="обойти все базы клиентов")
    retry.set_defaults(func=cmd_retry_outbox)

    alert = sub.add_parser("alert", help="служебное сообщение в Telegram и MAX")
    alert.add_argument("--text", default="")
    alert.set_defaults(func=cmd_alert)

    users_cmd = sub.add_parser("users", help="список пользователей")
    users_cmd.set_defaults(func=cmd_users)

    export_1c = sub.add_parser("export-1c", help="выгрузить заявку в файл для 1С и проверить формат")
    export_1c.add_argument("rfq_id", nargs="?", default="")
    export_1c.add_argument("--out", default="")
    export_1c.add_argument("--csv", action="store_true", help="выгрузить CSV вместо Excel")
    export_1c.set_defaults(func=cmd_export_1c)

    send_kp = sub.add_parser("send-kp", help="отправить готовое КП клиенту письмом")
    send_kp.add_argument("rfq_id")
    send_kp.add_argument("--to", default="")
    send_kp.add_argument("--body", default="")
    send_kp.set_defaults(func=cmd_send_kp)

    smtp_setup = sub.add_parser("smtp-setup", help="прописать SMTP в .env (пароль спрашивается скрыто)")
    smtp_setup.add_argument("--user", required=True, help="полный адрес почты")
    smtp_setup.add_argument("--host", default="smtp.rambler.ru")
    smtp_setup.add_argument("--port", type=int, default=465)
    smtp_setup.add_argument("--security", default="ssl", choices=("ssl", "starttls", "none"))
    smtp_setup.add_argument("--sender", default="")
    smtp_setup.add_argument("--env", default="")
    smtp_setup.set_defaults(func=cmd_smtp_setup)

    smtp_check = sub.add_parser("smtp-check", help="проверить настройки отправки почты")
    smtp_check.set_defaults(func=cmd_smtp_check)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

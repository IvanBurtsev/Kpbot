"""Личный кабинет клиента (B2B-портал покупателя).

Отдельный от админки поставщика вход и сессия: клиент видит только свой каталог,
корзину и заказы. Мультитенантность — база поставщика на клиента (tenant) плюс
жёсткая фильтрация по company_id из сессии.

Заказ из корзины превращается в обычную заявку поставщика (RFQ) с готовым
расчётом, поэтому попадает в ту же очередь, что и письма.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from urllib.parse import quote

from .cabinet_store import (
    ORDER_STATUSES,
    CabinetStore,
)
from .config import settings
from .domain import LineStatus, PriceItem, Quote, RfqLine, ValidationError, price_quote
from .pipeline import calc_settings, tenant_out_dir
from .mailer import MailError, Mailer
from .render import html_to_pdf, render_template
from .security import (
    RateLimiter,
    client_ip as resolve_client_ip,
    constant_time_equal,
    csrf_for,
    make_user_session,
    same_origin,
    session_issued_at,
    user_from_session,
    verify_csrf,
)
from .store import Store
from .tenancy import get_registry
from .tenant_ctx import CLIENT_TENANT_COOKIE, current_client_db

router = APIRouter(prefix="/cabinet", tags=["cabinet"])
CLIENT_COOKIE = "kpbot_client_session"
CSRF_FIELD = "csrf_token"

log = logging.getLogger("kpbot.cabinet")

_limiter = RateLimiter(settings.rate_limit_per_minute)


class OrderError(ValueError):
    """Заказ нельзя оформить: причина показывается покупателю."""


def cabinet_store() -> CabinetStore:
    """Данные кабинета живут в базе поставщика (тенанта), а не в основной."""
    store = CabinetStore(current_client_db(settings.db_path))
    store.init_schema()
    return store


def supplier_store() -> Store:
    """Прайс, остатки и реквизиты — из той же базы поставщика, что и кабинет."""
    return Store(current_client_db(settings.db_path))


# ---------- сессия и контекст ----------


def _session(request: Request) -> str | None:
    return request.cookies.get(CLIENT_COOKIE)


def _client_tenant_id(request: Request) -> str:
    """Код поставщика, в чей кабинет вошёл покупатель (для привязки сессии)."""
    code = user_from_session(
        settings.session_secret, request.cookies.get(CLIENT_TENANT_COOKIE), 86400 * 30
    )
    if code and _resolve_tenant(code):
        return code
    return "default"


def _changed_ts(value: object) -> float:
    """ISO-время смены пароля → epoch; пустое/битое — 0."""
    text = str(value or "")
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _user(request: Request) -> dict | None:
    token = _session(request)
    sub = user_from_session(settings.session_secret, token, settings.session_ttl_seconds)
    if not sub:
        return None
    # Вход покупателя привязан к поставщику: сессия одного кабинета не действует
    # в базе другого, даже если подменить куку клиента.
    tenant_id, _, user_id = sub.partition(":")
    if not user_id or tenant_id != _client_tenant_id(request):
        return None
    user = cabinet_store().user_by_id(user_id)
    if user is None:
        return None
    # Компания могла быть заблокирована после входа. Сессия живёт 8 часов, а
    # «отключить клиента» должно действовать сразу: иначе уволенный сотрудник
    # или должник продолжает видеть персональные цены, заказы и документы.
    company = cabinet_store().company(user["company_id"])
    if company is None or company["status"] != "active":
        return None
    changed = _changed_ts(user.get("password_changed_at"))
    if changed:
        issued = session_issued_at(settings.session_secret, token, settings.session_ttl_seconds)
        if issued is None or issued + 1 < changed:
            return None
    return user


def _company(request: Request, user: dict) -> dict | None:
    return cabinet_store().company(user["company_id"])


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/cabinet/login", status_code=status.HTTP_303_SEE_OTHER)


def _csrf(request: Request) -> str:
    return csrf_for(settings.session_secret, _session(request))


def _page(request: Request, template: str, *, code: int = 200, **context) -> HTMLResponse:
    user = context.pop("user", None) or _user(request)
    company = context.pop("company", None)
    if company is None and user:
        company = _company(request, user)
    body = render_template(
        template,
        request=request,
        user=user,
        company=company,
        csrf_token=_csrf(request),
        is_admin=bool(user and user["role"] == "client_admin"),
        **context,
    )
    return HTMLResponse(body, status_code=code)


def _check_csrf(request: Request, form) -> bool:
    return verify_csrf(settings.session_secret, _session(request), str(form.get(CSRF_FIELD, "")))


def _seller() -> dict:
    """Реквизиты поставщика для документов: интерфейс важнее .env, но .env — запасной."""
    saved = supplier_store().get_settings()
    defaults = {
        "company_name": settings.company_name,
        "company_inn": settings.company_inn,
        "company_kpp": settings.company_kpp,
        "company_address": settings.company_address,
        "company_phone": settings.company_phone,
        "company_email": settings.company_email,
        "company_signer": settings.company_signer,
        "company_account": settings.company_account,
        "company_bank": settings.company_bank,
        "company_bik": settings.company_bik,
        "company_corr_account": settings.company_corr_account,
    }
    return {key: saved.get(key) or value for key, value in defaults.items()}


def _fmt(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


# ---------- вход ----------


def _resolve_tenant(code: str) -> dict | None:
    """Клиент поставщика по коду из ссылки `/cabinet/login?t=<код>`."""
    code = (code or "").strip()
    if not code:
        return None
    tenant = get_registry(settings).get(code)
    if not tenant or tenant["status"] != "active":
        return None
    path = Path(tenant["db_path"])
    return tenant if path.exists() else None


def _cabinet_db_for_code(code: str) -> tuple[Path, str]:
    """База кабинета: по коду поставщика из ссылки или текущая по куке."""
    tenant = _resolve_tenant(code)
    if tenant is not None:
        return Path(tenant["db_path"]), tenant["id"]
    return current_client_db(settings.db_path), ""


def _find_cabinet_user(email: str, code: str = "") -> tuple[Path, str, dict | None]:
    """Ищет пользователя кабинета: указанный поставщик, текущий, затем все клиенты.

    Письмо со сбросом пароля открывают на телефоне или в другом браузере, где
    куки кабинета нет. Раньше в этом случае токен искали в основной базе и
    отвечали «ссылка недействительна» — восстановление доступа у клиентов
    поставщика (то есть в обычной мультитенантной установке) не работало.
    """
    path, tenant_code = _cabinet_db_for_code(code)
    user = CabinetStore(path).user_by_email(email) if email else None
    if user is not None or code:
        return path, tenant_code, user
    for tenant in get_registry(settings).list():
        if tenant.get("status") != "active":
            continue
        candidate = Path(tenant["db_path"])
        if not candidate.exists() or candidate == path:
            continue
        try:
            found = CabinetStore(candidate).user_by_email(email)
        except Exception:  # noqa: BLE001 - битая база клиента не должна ронять запрос
            continue
        if found is not None:
            return candidate, tenant["id"], found
    return path, tenant_code, None


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, flash: str = "", t: str = ""):
    if _user(request) is not None:
        return RedirectResponse("/cabinet", status_code=status.HTTP_303_SEE_OTHER)
    tenant = _resolve_tenant(t) if t else None
    return _page(
        request,
        "cabinet/login.html",
        code=404 if (t and tenant is None) else 200,
        flash=flash[:200],
        user=None,
        company=None,
        tenant_code=tenant["id"] if tenant else "",
        tenant_name=tenant["name"] if tenant else "",
        error="" if (not t or tenant) else "кабинет не найден или приостановлен",
    )


@router.post("/login")
async def login(request: Request):
    if not _limiter.allow(
        f"cabinet-login:{resolve_client_ip(request, settings.trusted_proxy_set)}"
    ):
        return _page(request, "cabinet/login.html", code=429,
                     error="слишком много попыток, подождите минуту", user=None, company=None,
                     tenant_code="", tenant_name="")
    if not same_origin(request, settings.public_base_url):
        return _page(request, "cabinet/login.html", code=403,
                     error="запрос пришёл с другого сайта", user=None, company=None,
                     tenant_code="", tenant_name="")
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    tenant_code = str(form.get("tenant", "")).strip()
    tenant = _resolve_tenant(tenant_code) if tenant_code else None
    if tenant_code and tenant is None:
        return _page(
            request,
            "cabinet/login.html",
            code=404,
            error="кабинет не найден или приостановлен",
            user=None,
            company=None,
            tenant_code="",
            tenant_name="",
        )

    # Код из ссылки важнее куки: поставщик мог прислать вход в другой кабинет.
    db_path = Path(tenant["db_path"]) if tenant else current_client_db(settings.db_path)
    store = CabinetStore(db_path)
    store.init_schema()
    user = store.authenticate(email, password)
    if user is None:
        return _page(request, "cabinet/login.html", code=401,
                     error="неверный логин или пароль", user=None, company=None,
                     tenant_code=tenant["id"] if tenant else "", tenant_name=tenant["name"] if tenant else "")
    company = store.company(user["company_id"])
    if company is None or company["status"] != "active":
        return _page(request, "cabinet/login.html", code=403,
                     error="доступ компании приостановлен, обратитесь к поставщику",
                     user=None, company=None,
                     tenant_code=tenant["id"] if tenant else "", tenant_name=tenant["name"] if tenant else "")
    token = make_user_session(
        settings.session_secret,
        f"{tenant['id'] if tenant else _client_tenant_id(request)}:{user['id']}",
        settings.session_ttl_seconds,
    )
    response = RedirectResponse("/cabinet", status_code=status.HTTP_303_SEE_OTHER)
    if tenant is not None:
        response.set_cookie(
            CLIENT_TENANT_COOKIE,
            make_user_session(settings.session_secret, tenant["id"], 86400 * 30),
            httponly=True,
            samesite="lax",
            secure=settings.public_base_url.lower().startswith("https"),
            max_age=86400 * 30,
            path="/",
        )
    elif db_path == settings.db_path:
        response.delete_cookie(CLIENT_TENANT_COOKIE, path="/")
    response.set_cookie(
        CLIENT_COOKIE, token, httponly=True, samesite="lax",
        secure=settings.public_base_url.lower().startswith("https"),
        max_age=settings.session_ttl_seconds, path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    response = RedirectResponse("/cabinet/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(CLIENT_COOKIE, path="/")
    response.delete_cookie(CLIENT_TENANT_COOKIE, path="/")
    return response


def _send_reset_email(address: str, link: str) -> bool:
    """Письмо со ссылкой сброса. Без SMTP просто не отправляем."""
    mailer = Mailer(settings)
    if not (mailer.enabled and mailer.host):
        log.warning("SMTP не настроен — ссылка сброса пароля не отправлена")
        return False
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = mailer.from_addr or settings.smtp_user or "no-reply@localhost"
    message["To"] = address
    message["Subject"] = "Восстановление пароля в кабинете kpbot"
    message.set_content(
        "Здравствуйте!\n\n"
        "Чтобы задать новый пароль, перейдите по ссылке (действует 60 минут):\n"
        f"{link}\n\n"
        "Если вы не запрашивали сброс — просто проигнорируйте это письмо.\n"
    )
    try:
        mailer.send(message, to=address)
        return True
    except MailError as exc:
        log.warning("письмо со сбросом пароля не ушло на %s: %s", address, exc)
        return False


@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request, flash: str = "", error: str = "", t: str = ""):
    tenant = _resolve_tenant(t) if t else None
    return _page(
        request, "cabinet/forgot.html", user=None, company=None,
        flash=flash[:200], error=error[:200],
        tenant_code=tenant["id"] if tenant else (t.strip() if t else ""),
        tenant_name=tenant["name"] if tenant else "",
    )


@router.post("/forgot")
async def forgot(request: Request):
    """Запрос ссылки сброса. Ответ одинаковый: адрес не подтверждаем."""
    if not same_origin(request, settings.public_base_url):
        return _page(
            request, "cabinet/forgot.html", code=403, user=None, company=None,
            error="запрос пришёл с другого сайта",
        )
    ip = resolve_client_ip(request, settings.trusted_proxy_set)
    if not _limiter.allow(f"cabinet-forgot:{ip}"):
        return _page(
            request, "cabinet/forgot.html", code=429, user=None, company=None,
            error="слишком много запросов, подождите минуту",
        )
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()[:200]
    code = str(form.get("tenant", "")).strip()[:64]
    path, tenant_code, user = _find_cabinet_user(email, code)
    if user:
        token = CabinetStore(path).create_password_reset(user["id"])
        link = f"{settings.public_base_url.rstrip('/')}/cabinet/reset?t={token}"
        if tenant_code:
            # Код поставщика в ссылке: иначе письмо, открытое без куки, ищет
            # токен не в той базе и отвечает «ссылка недействительна».
            link += f"&c={quote(tenant_code)}"
        # SMTP — блокирующий вызов: в async-обработчике он останавливает весь
        # сервер на время ответа почтового сервера.
        await run_in_threadpool(_send_reset_email, user["email"], link)
    return _page(
        request, "cabinet/forgot.html", user=None, company=None,
        tenant_code=tenant_code or code,
        flash="Если такой адрес есть в системе, письмо со ссылкой отправлено. Проверьте почту.",
    )


@router.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, t: str = "", c: str = "", error: str = ""):
    token = t[:200]
    path, tenant_code = _cabinet_db_for_code(c)
    store = CabinetStore(path)
    store.init_schema()
    if token and not store.password_reset_valid(token):
        return _page(
            request, "cabinet/reset.html", code=400, user=None, company=None,
            token="", tenant_code=c[:64],
            error="ссылка недействительна или истекла — запросите новую",
        )
    response = _page(
        request, "cabinet/reset.html", user=None, company=None,
        token=token, tenant_code=tenant_code or c[:64], error=error[:200],
    )
    if tenant_code:
        # Фиксируем кабинет в куке: после смены пароля вход и сброс должны
        # остаться у того же поставщика, даже если ссылку открыли без куки.
        response.set_cookie(
            CLIENT_TENANT_COOKIE,
            make_user_session(settings.session_secret, tenant_code, 86400 * 30),
            httponly=True,
            samesite="lax",
            secure=settings.public_base_url.lower().startswith("https"),
            max_age=86400 * 30,
            path="/",
        )
    return response


@router.post("/reset")
async def reset_password(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))[:200]
    code = str(form.get("tenant", "")).strip()[:64]
    path, tenant_code = _cabinet_db_for_code(code)
    store = CabinetStore(path)
    store.init_schema()
    new = str(form.get("new", ""))
    repeat = str(form.get("repeat", ""))
    if len(new) < 8:
        return _page(
            request, "cabinet/reset.html", code=400, user=None, company=None,
            token=token, tenant_code=tenant_code or code, error="пароль короче 8 символов",
        )
    if new != repeat:
        return _page(
            request, "cabinet/reset.html", code=400, user=None, company=None,
            token=token, tenant_code=tenant_code or code, error="пароли не совпадают",
        )
    user_id = store.consume_password_reset(token)
    if not user_id:
        return _page(
            request, "cabinet/reset.html", code=400, user=None, company=None,
            token="", tenant_code=tenant_code or code,
            error="ссылка недействительна или истекла — запросите новую",
        )
    store.set_password(user_id, new)
    params = [f"flash={quote('пароль изменён — войдите с новым')}"]
    if tenant_code:
        params.insert(0, f"t={quote(tenant_code)}")
    return RedirectResponse(
        "/cabinet/login?" + "&".join(params), status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, flash: str = "", error: str = ""):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    return _page(
        request,
        "cabinet/password.html",
        user=user,
        company=_company(request, user),
        flash=flash[:200],
        error=error[:300],
    )


@router.post("/password")
async def password_change(request: Request):
    """Смена своего пароля. После неё старая сессия недействительна."""
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    company = _company(request, user)
    current = str(form.get("current", ""))
    new = str(form.get("new", ""))
    repeat = str(form.get("repeat", ""))
    if new != repeat:
        return _page(
            request, "cabinet/password.html", code=400, user=user, company=company,
            error="новые пароли не совпадают",
        )
    if cabinet_store().authenticate(user["email"], current) is None:
        return _page(
            request, "cabinet/password.html", code=400, user=user, company=company,
            error="текущий пароль неверен",
        )
    try:
        cabinet_store().set_password(user["id"], new)
    except ValueError as exc:
        return _page(
            request, "cabinet/password.html", code=400, user=user, company=company, error=str(exc)
        )
    response = RedirectResponse(
        f"/cabinet/login?flash={quote('пароль изменён — войдите заново')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(CLIENT_COOKIE, path="/")
    return response


# ---------- дашборд ----------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    if company is None:
        return _login_redirect(request)
    orders = cabinet_store().list_orders(company["id"], limit=3)
    events = cabinet_store().recent_order_events(company["id"], 10)
    return _page(
        request, "cabinet/dashboard.html", user=user, company=company,
        orders=orders,
        events=events,
        debt=_fmt(company["debt"]),
        balance=_fmt(company["balance"]),
        credit=_fmt(company["credit_limit"]),
        available=_fmt(company["credit_limit"]) - _fmt(company["debt"]),
    )


# ---------- каталог ----------


def _catalog(company_id: str, query: str = "") -> list[dict]:
    supplier = supplier_store()
    cabinet = cabinet_store()
    personal = cabinet.price_map(company_id)
    packs = cabinet.pack_map()
    stock = supplier.stock_map()
    needle = (query or "").strip().lower()
    items: list[dict] = []
    for item in supplier.price_items():
        if needle and needle not in item.name.lower() and needle not in item.sku.lower():
            continue
        price = _fmt(personal.get(item.sku, item.price))
        stock_row = stock.get(item.sku) or {}
        available = _fmt(stock_row.get("qty")) if stock_row else None
        pack = int(packs.get(item.sku, 1) or 1)
        items.append({
            "sku": item.sku,
            "name": item.name,
            "unit": item.unit,
            "price": price,
            "base_price": _fmt(item.price),
            "personal": item.sku in personal,
            "pack": pack,
            "available": available,
            "can_order": (available is None) or (available >= pack),
        })
    return items


@router.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, q: str = "", page: int = 1, flash: str = ""):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    found = _catalog(company["id"], q)
    per_page = 50
    try:
        page_number = max(1, min(int(page or 1), 1000))
    except (TypeError, ValueError):
        page_number = 1
    start = (page_number - 1) * per_page
    items = found[start : start + per_page]
    cart_count = sum(item["qty"] for item in cabinet_store().cart_items(user["id"]))
    return _page(
        request,
        "cabinet/catalog.html",
        user=user,
        company=company,
        items=items,
        query=q.strip(),
        flash=flash[:200],
        cart_count=cart_count,
        total=len(found),
        page_number=page_number,
        has_next=start + per_page < len(found),
    )


# ---------- корзина ----------


def _max_qty_int() -> int:
    """Верхняя граница количества из настроек (Decimal → int, не меньше 1)."""
    try:
        value = int(settings.max_qty)
    except (TypeError, ValueError, OverflowError):
        value = 1_000_000
    return max(1, value)


def _qty_int(raw: object, default: int = 0) -> int:
    """Количество из формы: только целое в допустимом диапазоне.

    Раньше в корзину можно было положить 2 000 000 (или 10^19): страница
    корзины падала с ValidationError/OverflowError, и клиент «ломал» себе
    корзину, не понимая причины.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(value, _max_qty_int()))


def _cart_context(request: Request, user: dict, company: dict) -> dict:
    supplier = supplier_store()
    cabinet = cabinet_store()
    by_sku = {item.sku: item for item in supplier.price_items()}
    personal = cabinet.price_map(company["id"])
    packs = cabinet.pack_map()
    stock = supplier.stock_map()
    rows: list[dict] = []
    for entry in cabinet.cart_items(user["id"]):
        item = by_sku.get(entry["sku"])
        if item is None:
            cabinet.set_cart_qty(user["id"], entry["sku"], 0)
            continue
        qty = int(entry["qty"])
        if qty > _max_qty_int():
            # Старая корзина могла сохранить невероятное количество: лечим
            # строку, а не показываем 500 на каждом открытии.
            qty = _max_qty_int()
            cabinet.set_cart_qty(user["id"], entry["sku"], qty)
        if qty <= 0:
            cabinet.set_cart_qty(user["id"], entry["sku"], 0)
            continue
        price = _fmt(personal.get(item.sku, item.price))
        amount = price * qty
        stock_row = stock.get(item.sku) or {}
        rows.append({
            "sku": item.sku, "name": item.name, "unit": item.unit,
            "qty": qty, "price": price, "amount": amount,
            "pack": int(packs.get(item.sku, 1) or 1),
            "available": _fmt(stock_row.get("qty")) if stock_row else None,
            "vat_rate": item.vat_rate,
        })

    # Корзина считает те же деньги, что и заказ/КП: при ценах «с НДС» нельзя
    # начислять НДС повторно, а доставка должна быть видна до оформления.
    lines = [
        RfqLine(
            idx=index, raw_text=f"{row['name']} — {row['qty']} {row['unit']}",
            qty=Decimal(str(row["qty"])), unit=row["unit"],
            matched_sku=row["sku"], match_score=1.0, matched_name=row["name"],
            status=LineStatus.OK,
        )
        for index, row in enumerate(rows, start=1)
    ]
    catalogue = {
        row["sku"]: PriceItem(
            sku=row["sku"], name=row["name"], unit=row["unit"], price=row["price"],
            vat_rate=row.get("vat_rate"),
        )
        for row in rows
    }
    quote = price_quote(lines, catalogue, **calc_settings(supplier, settings).as_kwargs())
    return {
        "rows": rows,
        "subtotal": quote.subtotal,
        "vat": quote.vat,
        "delivery": quote.delivery,
        "delivery_vat": quote.delivery_vat,
        "total": quote.total,
        "prices_include_vat": quote.prices_include_vat,
    }


@router.get("/cart", response_class=HTMLResponse)
def cart(request: Request, flash: str = "", error: str = ""):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    context = _cart_context(request, user, company)
    return _page(
        request, "cabinet/cart.html", user=user, company=company,
        flash=flash[:200], error=error[:300], **context,
    )


@router.post("/cart/add")
async def cart_add(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    sku = str(form.get("sku", ""))
    qty = _qty_int(form.get("qty", "1"), default=0)
    cabinet = cabinet_store()
    pack = int(cabinet.pack_map().get(sku, 1) or 1)
    if qty <= 0:
        qty = pack
    qty = max(pack, ((qty + pack - 1) // pack) * pack)
    qty = min(qty, _max_qty_int())
    current = cabinet.cart_qty(user["id"], sku)
    cabinet.set_cart_qty(user["id"], sku, min(current + qty, _max_qty_int()))
    return RedirectResponse(f"/cabinet/catalog?flash={quote('добавлено в корзину')}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cart/qty")
async def cart_qty(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    sku = str(form.get("sku", ""))
    qty = _qty_int(form.get("qty", "0"), default=0)
    cabinet_store().set_cart_qty(user["id"], sku, qty)
    return RedirectResponse("/cabinet/cart", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cart/remove")
async def cart_remove(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    cabinet_store().set_cart_qty(user["id"], str(form.get("sku", "")), 0)
    return RedirectResponse("/cabinet/cart", status_code=status.HTTP_303_SEE_OTHER)


# ---------- оформление заказа ----------


def _validate_order(user: dict, company: dict) -> tuple[list[dict], Quote]:
    supplier = supplier_store()
    cabinet = cabinet_store()
    by_sku = {item.sku: item for item in supplier.price_items()}
    personal = cabinet.price_map(company["id"])
    packs = cabinet.pack_map()
    stock = supplier.stock_map()
    rows: list[dict] = []
    for entry in cabinet.cart_items(user["id"]):
        sku, qty = entry["sku"], int(entry["qty"])
        item = by_sku.get(sku)
        if item is None:
            raise OrderError(f"позиция {sku} больше не в прайсе — удалите её из корзины")
        pack = int(packs.get(sku, 1) or 1)
        if qty > settings.max_qty:
            # иначе расчёт падает внутри, и клиент видит 500 вместо объяснения
            raise OrderError(
                f"{item.name}: количество больше допустимого ({settings.max_qty:g} {item.unit})"
            )
        if qty <= 0 or qty % pack:
            raise OrderError(f"{item.name}: заказ кратен {pack} {item.unit}")
        if stock:
            if sku not in stock:
                raise OrderError(f"{item.name}: нет на складе")
            available = _fmt(stock[sku].get("qty"))
            if qty > available:
                raise OrderError(f"{item.name}: на складе только {available:g} {item.unit}")
        rows.append({
            "sku": sku, "name": item.name, "unit": item.unit, "qty": qty,
            "price": _fmt(personal.get(sku, item.price)), "amount": Decimal(0),
            "vat_rate": item.vat_rate,
        })
    if not rows:
        raise OrderError("корзина пуста")

    # Считаем заказ тем же price_quote, что и КП поставщика: иначе кабинет
    # показывал сумму без доставки и начислял НДС повторно при ценах «с НДС».
    lines = [
        RfqLine(
            idx=index, raw_text=f"{row['name']} — {row['qty']} {row['unit']}",
            qty=Decimal(str(row["qty"])), unit=row["unit"],
            matched_sku=row["sku"], match_score=1.0, matched_name=row["name"],
            status=LineStatus.OK,
        )
        for index, row in enumerate(rows, start=1)
    ]
    catalogue = {
        row["sku"]: PriceItem(
            sku=row["sku"], name=row["name"], unit=row["unit"], price=row["price"],
            vat_rate=row.get("vat_rate"),
        )
        for row in rows
    }
    quote = price_quote(lines, catalogue, **calc_settings(supplier, settings).as_kwargs())
    if len(quote.lines) != len(rows):
        raise OrderError("не все позиции корзины удалось рассчитать — обновите корзину")

    priced_rows = [
        {
            "sku": line.sku, "name": line.name, "unit": line.unit, "qty": line.qty,
            "price": line.price, "amount": line.amount,
        }
        for line in quote.lines
    ]
    return priced_rows, quote


def _rfq_from_order(
    user: dict,
    company: dict,
    rows: list[dict],
    order_number: str,
    quote: Quote,
    comment: str = "",
) -> tuple[str, str]:
    """Заказ ЛК → обычная заявка поставщика с готовым расчётом.

    Возвращает (id заявки, id снимка расчёта): снимок закрепляется за заказом,
    чтобы документы клиента не менялись, если поставщик потом правит заявку.
    """
    store = supplier_store()
    rfq_id = uuid.uuid4().hex
    lines = [
        RfqLine(
            idx=index, raw_text=f"{row['name']} — {row['qty']} {row['unit']}",
            qty=Decimal(str(row["qty"])), unit=row["unit"],
            matched_sku=row["sku"], match_score=1.0, matched_name=row["name"],
            status=LineStatus.OK,
        )
        for index, row in enumerate(rows, start=1)
    ]
    raw_text = "\n".join(line.raw_text for line in lines)
    if comment:
        # Комментарий клиента («нужна доставка до 12:00») — часть заказа, а не
        # примечание в кабинете: поставщик должен видеть его в карточке заявки.
        raw_text = f"{raw_text}\n\nКомментарий клиента: {comment}"
    store.save_rfq(
        rfq_id=rfq_id,
        message_id=f"cabinet:{uuid.uuid4().hex}",
        sender=f"{company['name']} <{user['email']}>",
        subject=f"Заказ из ЛК {order_number}",
        received_at=datetime.now(timezone.utc).isoformat(),
        status="draft_ready",
        raw_text=raw_text,
        lines=lines,
    )
    quote_id = uuid.uuid4().hex
    store.save_quote(quote_id, rfq_id, quote)
    store.log(rfq_id, "cabinet_order", f"client:{user['id']}", order_number)
    return rfq_id, quote_id


@router.post("/orders")
async def create_order(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    if company is None or company["status"] != "active":
        return RedirectResponse(f"/cabinet/cart?error={quote('компания заблокирована')}",
                                status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    comment = str(form.get("comment", "")).strip()[:500]
    try:
        rows, order_quote = _validate_order(user, company)
    except OrderError as exc:
        return RedirectResponse(f"/cabinet/cart?error={quote(str(exc))}",
                                status_code=status.HTTP_303_SEE_OTHER)

    cabinet = cabinet_store()
    # Номер «ЛК-…» выдаётся по максимуму существующих, но два одновременных
    # заказа всё равно могут получить один номер. Тогда INSERT отбивается
    # UNIQUE-индексом: пробуем следующий номер, а не отдаём клиенту 500.
    order: dict = {}
    for _attempt in range(3):
        number = cabinet.next_number()
        try:
            # Заказ создаём первым, а заявку — после: если заявка не создастся,
            # откатываем заказ. Обратный порядок оставлял «сиротскую» заявку
            # в очереди поставщика, о которой клиент ничего не знал.
            order = cabinet.create_order(
                company_id=company["id"], created_by=user["id"], number=number,
                comment=comment, total=str(order_quote.total),
                lines=[
                    {**row, "qty": str(row["qty"]), "price": str(row["price"]), "amount": str(row["amount"])}
                    for row in rows
                ],
            )
            break
        except sqlite3.IntegrityError:
            log.warning("номер заказа %s уже занят — пробую следующий", number)
            continue
    if not order:
        return RedirectResponse(
            f"/cabinet/cart?error={quote('не удалось оформить заказ, попробуйте ещё раз')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        rfq_id, quote_id = _rfq_from_order(
            user, company, rows, number, order_quote, comment=comment
        )
    except ValidationError as exc:
        # расчёт — последний рубеж: клиент должен увидеть причину, а не 500
        cabinet.delete_order(order["id"])
        return RedirectResponse(
            f"/cabinet/cart?error={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as exc:  # noqa: BLE001 - база поставщика может быть недоступна
        cabinet.delete_order(order["id"])
        log.warning("заказ ЛК %s: заявка не создана: %s", number, exc)
        return RedirectResponse(
            f"/cabinet/cart?error={quote('не удалось передать заявку поставщику, попробуйте ещё раз')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    cabinet.attach_rfq(order["id"], rfq_id=rfq_id, quote_id=quote_id)
    cabinet.clear_cart(user["id"])
    return RedirectResponse(f"/cabinet/orders/{order['id']}?flash={quote('заявка отправлена поставщику')}",
                            status_code=status.HTTP_303_SEE_OTHER)


# ---------- заказы ----------


@router.get("/orders", response_class=HTMLResponse)
def orders(request: Request):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    return _page(request, "cabinet/orders.html", user=user, company=company,
                 orders=cabinet_store().list_orders(company["id"]))


RFQ_STATUS_LABELS = {
    "received": "получена",
    "parsed": "разобрана",
    "priced": "рассчитана",
    "draft_ready": "КП готово",
    "human_review": "на проверке у поставщика",
    "approved": "КП одобрено",
    "sent": "КП отправлено",
    "rejected": "отклонена поставщиком",
}

# Допустимые переходы статуса заказа. «Отгружен» и «отменён» — терминальные:
# вернуть отгруженный заказ в «ожидает» нельзя, иначе документы (счёт, УПД)
# и история начинают противоречить друг другу. Пока статус приходил из 1С
# без проверки, отменённый заказ можно было «отгрузить» задним числом.
ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing", "shipped", "cancelled"}),
    "processing": frozenset({"shipped", "cancelled"}),
    "shipped": frozenset(),
    "cancelled": frozenset(),
}


def order_transition_error(current: str, new: str) -> str:
    """Пустая строка — переход допустим; иначе текст для человека."""
    allowed = ORDER_TRANSITIONS.get(current)
    if allowed is None:
        return f"неизвестный текущий статус заказа: {current}"
    if new not in allowed:
        return (
            f"нельзя перевести заказ из «{ORDER_STATUS_LABELS.get(current, current)}» "
            f"в «{ORDER_STATUS_LABELS.get(new, new)}»"
        )
    return ""


def _send_order_status_email(cabinet: CabinetStore, order: dict, new_status: str, *, store) -> int:
    """Письмо клиенту о смене статуса. Сбой не ломает смену статуса."""
    mailer = Mailer(settings)
    if not (settings.cabinet_notify_email and mailer.enabled and mailer.host):
        return 0
    recipients = [user["email"] for user in cabinet.list_users(order["company_id"])]
    if not recipients:
        return 0
    label = ORDER_STATUS_LABELS.get(new_status, new_status)
    link = f"{settings.public_base_url.rstrip('/')}/cabinet/orders/{order['id']}"
    message = (
        f"Здравствуйте!\n\n"
        f"Статус вашего заказа {order['number']} изменился: {label}.\n"
        f"Открыть заказ: {link}\n\n"
        f"— {_seller().get('company_name')}"
    )
    sent = 0
    for address in recipients:
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["From"] = mailer.from_addr or settings.smtp_user or "no-reply@localhost"
        mail["To"] = address
        mail["Subject"] = f"Заказ {order['number']}: {label}"
        mail.set_content(message)
        try:
            mailer.send(mail, to=address)
            sent += 1
        except MailError as exc:
            log.warning("письмо о статусе заказа %s не ушло на %s: %s", order["number"], address, exc)
    if sent:
        store.log_event("client_order_notified", "1c", f"{order['number']}: писем {sent}")
    return sent


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_page(request: Request, order_id: str, flash: str = ""):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    order = cabinet_store().order(order_id)
    if order is None or order["company_id"] != company["id"]:
        return _page(request, "cabinet/orders.html", code=404, user=user, company=company,
                     orders=cabinet_store().list_orders(company["id"]), error="заказ не найден",
                     flash="")
    # Статус заказа в ЛК и статус заявки у поставщика живут отдельно: клиенту
    # важно видеть оба, иначе «Ожидает» висит даже после отправки КП.
    rfq = None
    quote = None
    if order.get("rfq_id"):
        rfq = supplier_store().get_rfq(order["rfq_id"])
        quote = supplier_store().get_quote_by_id(order.get("quote_id") or "")
        if quote is None:
            quote = supplier_store().get_quote(order["rfq_id"])
    rfq_status = (rfq or {}).get("status", "")
    # «Итого» включает доставку и НДС: показываем их отдельными строками,
    # иначе таблица позиций не складывается в итог и выглядит как переплата.
    totals = {
        "vat": _fmt((quote or {}).get("vat")),
        "delivery": _fmt((quote or {}).get("delivery")),
        "include_vat": bool(int((quote or {}).get("prices_include_vat") or 0)),
    }
    return _page(request, "cabinet/order.html", user=user, company=company,
                 order=order, flash=flash[:200], seller=_seller(),
                 totals=totals,
                 rfq_status=rfq_status,
                 rfq_status_label=RFQ_STATUS_LABELS.get(rfq_status, ""))


DOC_TITLES = {
    "invoice": "Счёт на оплату",
    "kp": "Коммерческое предложение",
    "upd": "Универсальный передаточный документ",
}


def _doc_pdf_fresh(pdf_path: Path, order: dict) -> bool:
    """PDF уже собран из этого снимка заказа и текущего шаблона.

    Печать headless-браузером на каждый просмотр документа — это процесс на
    десятки мегабайт и до 45 секунд; авторизованный клиент мог дергать свой
    счёт сколько угодно и загрузить сервер. Снимок заказа и шаблон неизменны —
    можно отдать готовый файл. Изменение заказа (updated_at) или шаблона
    делает кэш недействительным.
    """
    try:
        pdf_stat = pdf_path.stat()
    except OSError:
        return False
    if pdf_stat.st_size <= 1024:
        return False
    template = Path(settings.templates_dir) / "cabinet" / "doc.html"
    try:
        template_ts = template.stat().st_mtime
    except OSError:
        template_ts = 0.0
    try:
        updated_ts = datetime.fromisoformat(
            str(order.get("updated_at") or "").replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        updated_ts = 0.0
    return pdf_stat.st_mtime >= max(template_ts, updated_ts)


@router.get("/orders/{order_id}/doc/{kind}")
def order_doc(request: Request, order_id: str, kind: str):
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    order = cabinet_store().order(order_id)
    if order is None or order["company_id"] != company["id"]:
        return PlainTextResponse("заказ не найден", status_code=404)
    kind = kind if kind in DOC_TITLES else "invoice"
    seller = _seller()
    supplier = supplier_store()
    # Снимок расчёта, закреплённый за заказом. У заказов, созданных до появления
    # поля, берём последний — иначе документ остался бы без итогов вовсе.
    quote = supplier.get_quote_by_id(order.get("quote_id") or "")
    if quote is None and order.get("rfq_id"):
        quote = supplier.get_quote(order["rfq_id"])
    rate = calc_settings(supplier, settings).vat_rate
    lines = order["lines"]
    breakdown: list[tuple[Decimal, Decimal, Decimal]] = []
    if quote:
        # Документ берёт итоги и построчный НДС из снимка КП. Строки заказа —
        # снимок на момент заказа, поэтому сумма строк, НДС и «Итого» сходятся
        # до копейки и не меняются после правок заявки поставщиком.
        rate = _fmt(quote.get("vat_rate") or rate)
        inclusive = bool(int(quote.get("prices_include_vat") or 0))
        delivery = _fmt(quote.get("delivery"))
        delivery_vat = _fmt(quote.get("delivery_vat"))
        snapshot = {line["sku"]: line for line in quote.get("lines", [])}
        built: list[dict] = []
        for line in order["lines"]:
            qty = _fmt(line.get("qty")) or Decimal("1")
            snap = snapshot.get(line["sku"])
            net = vat = Decimal("0")
            net_price = _fmt(line.get("price"))
            if snap is not None:
                amount = _fmt(snap.get("amount"))
                net = _fmt(snap.get("net"))
                vat = _fmt(snap.get("vat"))
                if net == 0 and amount != 0 and not (rate == 0 and vat == 0):
                    # снимок старого формата: построчного НДС в нём нет
                    if inclusive and rate:
                        net = (amount / (Decimal("1") + rate)).quantize(Decimal("0.01"))
                        vat = amount - net
                    else:
                        net = amount
                        vat = (net * rate).quantize(Decimal("0.01"))
                net_price = _fmt(snap.get("price"))
                if inclusive and rate:
                    net_price = (net_price / (Decimal("1") + rate)).quantize(Decimal("0.01"))
            else:
                # заказ без снимка расчёта (старые данные)
                amount = _fmt(line.get("amount"))
                if inclusive and rate:
                    net = (amount / (Decimal("1") + rate)).quantize(Decimal("0.01"))
                    vat = amount - net
                    net_price = (net_price / (Decimal("1") + rate)).quantize(Decimal("0.01"))
                else:
                    net = amount
                    vat = (net * rate).quantize(Decimal("0.01"))
            built.append({
                **line,
                "price": net_price,
                "amount": net,
                "net": net,
                "vat": vat,
                "gross": net + vat,
                "vat_rate": _fmt(snap.get("vat_rate")) if snap and snap.get("vat_rate") not in (None, "") else rate,
            })
        lines = built
        if delivery:
            if inclusive and rate:
                delivery_net = delivery - delivery_vat
            else:
                delivery_net = delivery
            lines.append({
                "sku": "", "name": "Доставка", "unit": "", "qty": 1,
                "price": delivery_net, "amount": delivery_net,
                "net": delivery_net, "vat": delivery_vat,
                "gross": delivery_net + delivery_vat, "vat_rate": rate,
            })
        net_total = _fmt(quote.get("subtotal")) + (delivery - delivery_vat if inclusive else delivery)
        vat_total = _fmt(quote.get("vat"))
        total = _fmt(quote.get("total"))
        buckets: dict[Decimal, list[Decimal]] = {}
        for line in lines:
            line_rate = _fmt(line.get("vat_rate")) if line.get("vat_rate") not in (None, "") else rate
            bucket = buckets.setdefault(line_rate, [Decimal("0"), Decimal("0")])
            bucket[0] += _fmt(line.get("net"))
            bucket[1] += _fmt(line.get("vat"))
        breakdown = [
            (line_rate, values[0], values[1])
            for line_rate, values in sorted(buckets.items(), key=lambda pair: pair[0], reverse=True)
            if values[1]
        ]
    else:
        net_total = sum((_fmt(line["amount"]) for line in order["lines"]), Decimal(0))
        total = _fmt(order["total"])
        vat_total = total - net_total
    order = {**order, "lines": lines}
    issued = (order["created_at"] or "")[:10]
    try:
        valid_until = (date.fromisoformat(issued) + timedelta(days=settings.quote_valid_days)).isoformat()
    except ValueError:
        valid_until = issued
    body = render_template(
        "cabinet/doc.html",
        order=order,
        company=company,
        seller=seller,
        title=DOC_TITLES[kind],
        kind=kind,
        net_total=net_total,
        vat_total=vat_total,
        total=total,
        vat_rate=rate,
        breakdown=breakdown,
        valid_until=valid_until,
    )
    html_path = tenant_out_dir(settings, supplier_store().db_path) / f"cabinet-{order_id}-{kind}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path = html_path.with_suffix(".pdf")
    if _doc_pdf_fresh(pdf_path, order):
        return Response(content=pdf_path.read_bytes(), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{kind}-{order_id[:8]}.pdf"'})
    html_path.write_text(body, encoding="utf-8")
    ok, _reason = html_to_pdf(html_path, pdf_path) if settings.pdf_enabled else (False, "off")
    if ok and pdf_path.exists():
        return Response(content=pdf_path.read_bytes(), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{kind}-{order_id[:8]}.pdf"'})
    return HTMLResponse(body, headers={"Content-Disposition": f'inline; filename="{kind}-{order_id[:8]}.html"'})


@router.get("/documents/act")
def reconciliation_act(request: Request, start: str = "", end: str = ""):
    """Акт сверки за период — для client_admin."""
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    company = _company(request, user)
    if user["role"] != "client_admin":
        return PlainTextResponse("акт сверки доступен администратору компании", status_code=403)
    today = date.today()
    period_start = (start or f"{today.year}-01-01")[:10]
    period_end = (end or today.isoformat())[:10]
    all_orders = cabinet_store().orders_between(company["id"], period_start, period_end)
    # Отменённые заказы в сверку не идут: это не оборот, а отмена.
    cancelled = [order for order in all_orders if order["status"] == "cancelled"]
    orders = [order for order in all_orders if order["status"] != "cancelled"]
    turnover = sum((_fmt(order["total"]) for order in orders), Decimal(0))
    body = render_template(
        "cabinet/act.html",
        company=company,
        orders=orders,
        seller=_seller(),
        total=turnover,
        turnover=turnover,
        period_start=period_start,
        period_end=period_end,
        excluded=len(cancelled),
        balance=_fmt(company.get("balance")),
        debt=_fmt(company.get("debt")),
        credit_limit=_fmt(company.get("credit_limit")),
    )
    return HTMLResponse(body, headers={"Content-Disposition": 'inline; filename="act.html"'})


@router.post("/orders/{order_id}/repeat")
async def repeat_order(request: Request, order_id: str):
    """Повторить заказ: позиции возвращаются в корзину."""
    user = _user(request)
    if user is None:
        return _login_redirect(request)
    form = await request.form()
    if not _check_csrf(request, form):
        return PlainTextResponse("csrf failed", status_code=403)
    company = _company(request, user)
    order = cabinet_store().order(order_id)
    if order is None or order["company_id"] != company["id"]:
        return RedirectResponse("/cabinet/orders", status_code=status.HTTP_303_SEE_OTHER)
    cabinet = cabinet_store()
    packs = cabinet.pack_map()
    for line in order["lines"]:
        step = max(1, int(packs.get(line["sku"], 1) or 1))
        qty = int(line["qty"])
        qty = max(step, ((qty + step - 1) // step) * step)
        cabinet.set_cart_qty(user["id"], line["sku"], cabinet.cart_qty(user["id"], line["sku"]) + qty)
    return RedirectResponse(
        f"/cabinet/cart?flash={quote('позиции последнего заказа в корзине')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------- API-заглушки для 1С ----------


def _api_allowed(request: Request) -> bool:
    """Ключ владельца или ключ конкретного клиента (вместе с X-Tenant).

    Если запрос адресован клиенту (X-Tenant), принимается только ключ этого
    клиента. Общий ключ владельца сюда не подставляется: иначе его утечка
    открывала бы заказы и финансы всех клиентов, а не одну базу.
    """
    key = request.headers.get("X-API-Key", "")
    if not key:
        return False
    code = (request.headers.get("X-Tenant") or "").strip()
    if code:
        tenant = _resolve_tenant(code)
        return bool(tenant and get_registry(settings).verify_key(tenant["id"], key))
    return constant_time_equal(key, settings.api_key)


def _api_tenant_path(request: Request) -> Path | None:
    """База из заголовка `X-Tenant`; без заголовка — текущая/основная."""
    code = (request.headers.get("X-Tenant") or "").strip()
    if not code:
        return current_client_db(settings.db_path)
    tenant = _resolve_tenant(code)
    return Path(tenant["db_path"]) if tenant else None


@router.get("/api/1c/stock")
def api_stock(request: Request):
    if not _api_allowed(request):
        return PlainTextResponse("unauthorized", status_code=401)
    path = _api_tenant_path(request)
    if path is None:
        return PlainTextResponse("tenant not found", status_code=404)
    stock = Store(path).stock_map()
    return JSONResponse([{"sku": sku, **row} for sku, row in stock.items()])


@router.get("/api/1c/prices")
def api_prices(request: Request):
    if not _api_allowed(request):
        return PlainTextResponse("unauthorized", status_code=401)
    path = _api_tenant_path(request)
    if path is None:
        return PlainTextResponse("tenant not found", status_code=404)
    items = Store(path).price_items()
    return JSONResponse([{"sku": i.sku, "name": i.name, "unit": i.unit, "price": str(i.price)} for i in items])


@router.get("/api/1c/orders")
def api_orders(request: Request):
    if not _api_allowed(request):
        return PlainTextResponse("unauthorized", status_code=401)
    path = _api_tenant_path(request)
    if path is None:
        return PlainTextResponse("tenant not found", status_code=404)
    cabinet = CabinetStore(path)
    cabinet.init_schema()
    return JSONResponse([
        {"id": order["id"], "number": order["number"], "company": order["company_name"],
         "status": order["status"], "total": order["total"], "created_at": order["created_at"]}
        for order in cabinet.all_orders()
    ])


@router.post("/api/1c/orders/{order_id}/status")
async def api_order_status(request: Request, order_id: str):
    if not _api_allowed(request):
        return PlainTextResponse("unauthorized", status_code=401)
    new_status = ""
    if "application/json" in request.headers.get("content-type", ""):
        try:
            payload = await request.json()
            new_status = str(payload.get("status", ""))
        except Exception:  # noqa: BLE001
            new_status = ""
    else:
        form = await request.form()
        new_status = str(form.get("status", "")) if form else ""
    if new_status not in ORDER_STATUSES:
        return JSONResponse({"ok": False, "error": "неизвестный статус"}, status_code=422)
    path = _api_tenant_path(request)
    if path is None:
        return PlainTextResponse("tenant not found", status_code=404)
    cabinet = CabinetStore(path)
    cabinet.init_schema()
    order = cabinet.order(order_id)
    if order is None:
        return JSONResponse({"ok": False, "error": "заказ не найден"}, status_code=404)
    error = order_transition_error(order["status"], new_status)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=409)
    cabinet.update_status(order_id, new_status)
    cabinet.log_order_event(order_id, order["company_id"], new_status)
    # Клиент должен узнать об отгрузке так же, как при смене статуса в админке.
    sent = 0
    try:
        sent = await run_in_threadpool(
            _send_order_status_email, cabinet, order, new_status, store=Store(path)
        )
    except Exception as exc:  # noqa: BLE001 - уведомление не должно ломать статус
        log.warning("уведомление о статусе заказа %s не отправлено: %s", order_id, exc)
    return JSONResponse({"ok": True, "status": new_status, "notified": sent})


@router.post("/api/1c/settlements")
async def api_settlements(request: Request):
    if not _api_allowed(request):
        return PlainTextResponse("unauthorized", status_code=401)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "нужен JSON"}, status_code=422)
    company_id = str(payload.get("company_id", ""))
    if not company_id:
        return JSONResponse({"ok": False, "error": "нужен company_id"}, status_code=422)
    path = _api_tenant_path(request)
    if path is None:
        return PlainTextResponse("tenant not found", status_code=404)
    store = CabinetStore(path)
    store.init_schema()
    if store.company(company_id) is None:
        return JSONResponse({"ok": False, "error": "компания не найдена"}, status_code=404)
    store.update_finance(
        company_id,
        balance=payload.get("balance"),
        credit_limit=payload.get("credit_limit"),
        debt=payload.get("debt"),
        status=payload.get("status"),
    )
    return JSONResponse({"ok": True})

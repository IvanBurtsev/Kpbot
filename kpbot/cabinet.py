"""Личный кабинет клиента (B2B-портал покупателя).

Отдельный от админки поставщика вход и сессия: клиент видит только свой каталог,
корзину и заказы. Мультитенантность — база поставщика на клиента (tenant) плюс
жёсткая фильтрация по company_id из сессии.

Заказ из корзины превращается в обычную заявку поставщика (RFQ) с готовым
расчётом, поэтому попадает в ту же очередь, что и письма.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from urllib.parse import quote

from .cabinet_store import (
    ORDER_STATUSES,
    CabinetStore,
)
from .config import settings
from .domain import LineStatus, PriceItem, RfqLine, price_quote
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
    user_id = user_from_session(settings.session_secret, token, settings.session_ttl_seconds)
    if not user_id:
        return None
    user = cabinet_store().user_by_id(user_id)
    if user is None:
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
    token = make_user_session(settings.session_secret, user["id"], settings.session_ttl_seconds)
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
def logout(request: Request):
    response = RedirectResponse("/cabinet/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(CLIENT_COOKIE, path="/")
    response.delete_cookie(CLIENT_TENANT_COOKIE, path="/")
    return response


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
    return _page(
        request, "cabinet/dashboard.html", user=user, company=company,
        orders=orders,
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
        if len(items) >= 300:
            break
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


def _cart_context(request: Request, user: dict, company: dict) -> dict:
    supplier = supplier_store()
    cabinet = cabinet_store()
    by_sku = {item.sku: item for item in supplier.price_items()}
    personal = cabinet.price_map(company["id"])
    packs = cabinet.pack_map()
    stock = supplier.stock_map()
    rows: list[dict] = []
    total = Decimal(0)
    vat = settings.vat_rate
    for entry in cabinet.cart_items(user["id"]):
        item = by_sku.get(entry["sku"])
        if item is None:
            cabinet.set_cart_qty(user["id"], entry["sku"], 0)
            continue
        qty = int(entry["qty"])
        price = _fmt(personal.get(item.sku, item.price))
        amount = price * qty
        total += amount
        stock_row = stock.get(item.sku) or {}
        rows.append({
            "sku": item.sku, "name": item.name, "unit": item.unit,
            "qty": qty, "price": price, "amount": amount,
            "pack": int(packs.get(item.sku, 1) or 1),
            "available": _fmt(stock_row.get("qty")) if stock_row else None,
        })
    return {
        "rows": rows,
        "subtotal": total,
        "vat": (total * vat).quantize(Decimal("0.01")),
        "total": (total * (1 + vat)).quantize(Decimal("0.01")),
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
    try:
        qty = int(str(form.get("qty", "1")))
    except ValueError:
        qty = 0
    cabinet = cabinet_store()
    pack = cabinet.pack_map().get(sku, 1)
    qty = max(pack, ((qty + pack - 1) // pack) * pack)
    cabinet.set_cart_qty(user["id"], sku, cabinet.cart_qty(user["id"], sku) + qty)
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
    try:
        qty = int(str(form.get("qty", "0")))
    except ValueError:
        qty = 0
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


def _validate_order(user: dict, company: dict) -> tuple[list[dict], Decimal, Decimal]:
    supplier = supplier_store()
    cabinet = cabinet_store()
    by_sku = {item.sku: item for item in supplier.price_items()}
    personal = cabinet.price_map(company["id"])
    packs = cabinet.pack_map()
    stock = supplier.stock_map()
    rows: list[dict] = []
    subtotal = Decimal(0)
    for entry in cabinet.cart_items(user["id"]):
        sku, qty = entry["sku"], int(entry["qty"])
        item = by_sku.get(sku)
        if item is None:
            raise OrderError(f"позиция {sku} больше не в прайсе — удалите её из корзины")
        pack = int(packs.get(sku, 1) or 1)
        if qty <= 0 or qty % pack:
            raise OrderError(f"{item.name}: заказ кратен {pack} {item.unit}")
        if stock:
            if sku not in stock:
                raise OrderError(f"{item.name}: нет на складе")
            available = _fmt(stock[sku].get("qty"))
            if qty > available:
                raise OrderError(f"{item.name}: на складе только {available:g} {item.unit}")
        price = _fmt(personal.get(sku, item.price))
        amount = price * qty
        subtotal += amount
        rows.append({
            "sku": sku, "name": item.name, "unit": item.unit, "qty": qty,
            "price": price, "amount": amount,
        })
    if not rows:
        raise OrderError("корзина пуста")
    vat = (subtotal * settings.vat_rate).quantize(Decimal("0.01"))
    return rows, subtotal, subtotal + vat


def _rfq_from_order(user: dict, company: dict, rows: list[dict], order_number: str) -> str:
    """Заказ ЛК → обычная заявка поставщика с готовым расчётом."""
    store = supplier_store()
    rfq_id = uuid.uuid4().hex
    lines = [
        RfqLine(
            idx=index, raw_text=f"{row['name']} — {row['qty']} {row['unit']}",
            qty=Decimal(row["qty"]), unit=row["unit"],
            matched_sku=row["sku"], match_score=1.0, matched_name=row["name"],
            status=LineStatus.OK,
        )
        for index, row in enumerate(rows, start=1)
    ]
    store.save_rfq(
        rfq_id=rfq_id,
        message_id=f"cabinet:{uuid.uuid4().hex}",
        sender=f"{company['name']} <{user['email']}>",
        subject=f"Заказ из ЛК {order_number}",
        received_at=datetime.now(timezone.utc).isoformat(),
        status="draft_ready",
        raw_text="\n".join(line.raw_text for line in lines),
        lines=lines,
    )
    catalogue = {
        row["sku"]: PriceItem(sku=row["sku"], name=row["name"], unit=row["unit"], price=row["price"])
        for row in rows
    }
    quote = price_quote(lines, catalogue, vat_rate=settings.vat_rate)
    store.save_quote(uuid.uuid4().hex, rfq_id, quote)
    store.log(rfq_id, "cabinet_order", f"client:{user['id']}", order_number)
    return rfq_id


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
        rows, subtotal, total = _validate_order(user, company)
    except OrderError as exc:
        return RedirectResponse(f"/cabinet/cart?error={quote(str(exc))}",
                                status_code=status.HTTP_303_SEE_OTHER)

    cabinet = cabinet_store()
    number = cabinet.next_number()
    rfq_id = _rfq_from_order(user, company, rows, number)
    order = cabinet.create_order(
        company_id=company["id"], created_by=user["id"], number=number,
        comment=comment, total=str(total),
        lines=[
            {**row, "price": str(row["price"]), "amount": str(row["amount"])}
            for row in rows
        ],
        rfq_id=rfq_id,
    )
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
    if order.get("rfq_id"):
        rfq = supplier_store().get_rfq(order["rfq_id"])
    rfq_status = (rfq or {}).get("status", "")
    return _page(request, "cabinet/order.html", user=user, company=company,
                 order=order, flash=flash[:200], seller=_seller(),
                 rfq_status=rfq_status,
                 rfq_status_label=RFQ_STATUS_LABELS.get(rfq_status, ""))


DOC_TITLES = {
    "invoice": "Счёт на оплату",
    "kp": "Коммерческое предложение",
    "upd": "Универсальный передаточный документ",
}


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
    net_total = sum((_fmt(line["amount"]) for line in order["lines"]), Decimal(0))
    total = _fmt(order["total"])
    vat_total = total - net_total
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
        vat_rate=settings.vat_rate,
        valid_until=valid_until,
    )
    html_path = settings.out_dir / f"cabinet-{order_id}-{kind}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(body, encoding="utf-8")
    pdf_path = html_path.with_suffix(".pdf")
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
    all_orders = cabinet_store().list_orders(company["id"], limit=500)
    # Отменённые заказы в сверку не идут: это не оборот, а отмена.
    cancelled = [order for order in all_orders if order["status"] == "cancelled"]
    orders = [
        order
        for order in all_orders
        if order["status"] != "cancelled" and period_start <= (order["created_at"] or "")[:10] <= period_end
    ]
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
    """Ключ владельца или ключ конкретного клиента (вместе с X-Tenant)."""
    key = request.headers.get("X-API-Key", "")
    if not key:
        return False
    code = (request.headers.get("X-Tenant") or "").strip()
    if code:
        tenant = _resolve_tenant(code)
        if tenant and get_registry(settings).verify_key(tenant["id"], key):
            return True
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
    cabinet.update_status(order_id, new_status)
    return JSONResponse({"ok": True, "status": new_status})


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

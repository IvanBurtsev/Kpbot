"""HTTP-слой: интерфейс рекрутера-продавца и версионированное API.

Принципы:
  * интерфейс закрыт паролем (в письмах ПДн), API — ключом в заголовке;
  * любая форма проверяет CSRF; cookie HttpOnly + SameSite=Lax;
  * размер тела ограничен до чтения файла;
  * весь пользовательский текст выводится через autoescape.
"""

from __future__ import annotations

import base64
import logging
import re
import sqlite3
import secrets
import string
import time
import uuid
from dataclasses import replace
from datetime import datetime
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .cabinet import order_transition_error, router as cabinet_router
from .cabinet_store import ORDER_STATUS_LABELS, ORDER_STATUSES, CabinetStore
from .config import LEGAL_VAT_RATES, settings
from .domain import LineStatus, RfqLine, RfqStatus, ValidationError, quantity
from .export import build_rfq_xlsx, check_1c_sheet
from .mailer import MailError, Mailer, build_kp_message, load_attachments, reply_subject
from .mailparse import sender_address, sender_display_name
from .one_c import OneCError, client_from_settings
from .pipeline import (
    NotAnRfq,
    Pipeline,
    build_reply_text_from_store,
    legacy_vat_rates,
    rfq_lines,
)
from .pricelist import load_price_items, load_stock_items, merge_prices
from .render import render_template
from .report import build_pilot_report
from .security import (
    CSRF_FIELD,
    SESSION_COOKIE,
    RateLimiter,
    client_ip as resolve_client_ip,
    constant_time_equal,
    csrf_for,
    make_session,
    make_user_session,
    same_origin,
    session_issued_at,
    user_from_session,
    verify_csrf,
    verify_session,
)
from .users import ROLE_ADMIN, User, normalize_email
from .store import Store
from .tenancy import get_registry
from .tenant_ctx import CLIENT_TENANT_COOKIE, client_db, current_supplier_db, supplier_db

from .logs import mask, register_settings_secrets, setup_logging

setup_logging()
register_settings_secrets(settings)
log = logging.getLogger("kpbot.web")

app = FastAPI(
    title="kpbot", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json"
)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.include_router(cabinet_router)

settings.ensure_dirs()
registry = get_registry(settings)


class TenantStore:
    """Фасад над Store: база выбирается по клиенту из контекста запроса."""

    def __init__(self, default_path: Path) -> None:
        self.default_path = default_path

    def _target(self) -> Store:
        return Store(current_supplier_db(self.default_path))

    def __getattr__(self, name: str):
        return getattr(self._target(), name)


store = TenantStore(settings.db_path)
pipeline = Pipeline(store, settings)
mailer = Mailer(settings)
limiter = RateLimiter(settings.rate_limit_per_minute)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@app.middleware("http")
async def hardening(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_upload_bytes + 128 * 1024:
        return PlainTextResponse("файл слишком большой — уменьшите размер", status_code=413)

    # Кука клиента есть, а базы нет (удалена, переименована, клиент приостановлен):
    # запрос нельзя молча переводить в основную базу — это показало бы одному
    # поставщику данные другого. Но и страницу входа блокировать нельзя: со
    # «зависшей» кукой человек не сможет войти вообще.
    stale_tenant = bool(request.cookies.get(TENANT_COOKIE)) and tenant_db_from_request(request) is None
    stale_client = (
        bool(request.cookies.get(CLIENT_TENANT_COOKIE))
        and client_tenant_db_from_request(request) is None
    )
    login_path = request.url.path in ("/ui/login", "/cabinet/login")
    admin_surface = request.url.path.startswith("/ui")
    cabinet_surface = request.url.path.startswith("/cabinet")
    # Испорченная кука другой поверхности не должна выкидывать из этой:
    # «протухшая» кука кабинета на /ui раньше отправляла владельца на вход
    # кабинета и удаляла клиентскую куку.
    if not login_path and stale_tenant and admin_surface:
        log.warning("кука поставщика не разрешилась (%s) — на вход", request.url.path)
        response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(TENANT_COOKIE, path="/")
        return response
    if not login_path and stale_client and cabinet_surface:
        log.warning("кука кабинета не разрешилась (%s) — на вход", request.url.path)
        response = RedirectResponse("/cabinet/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(CLIENT_TENANT_COOKIE, path="/")
        return response

    tenant_token = supplier_db.set(request_tenant_path(request))
    client_token = client_db.set(client_tenant_db_from_request(request))
    request_token = _CURRENT_REQUEST.set(request)
    try:
        response = await call_next(request)
    finally:
        _CURRENT_REQUEST.reset(request_token)
        client_db.reset(client_token)
        supplier_db.reset(tenant_token)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if login_path and request.method == "GET" or (admin_surface and stale_client) or (
        cabinet_surface and stale_tenant
    ):
        # на странице входа забываем испорченную куку; при успешном входе новая
        # кука выставляется обработчиком, и затирать её нельзя
        if stale_tenant:
            response.delete_cookie(TENANT_COOKIE, path="/")
        if stale_client:
            response.delete_cookie(CLIENT_TENANT_COOKIE, path="/")
    if settings.public_base_url.lower().startswith("https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Request-ID", request_id)
    if request.url.path.startswith(("/ui", "/cabinet")):
        # В интерфейсе и кабинете бывают пароли API-ключи, ПДн и суммы:
        # браузерный кэш и BFCache не должны их хранить.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "req id=%s %s %s -> %s %.0fms",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.on_event("startup")
async def check_production_config() -> None:
    registry.ensure_default()
    created = store.ensure_default_admin(settings.ui_password)
    if created is not None:
        log.info("создан администратор %s (пароль — из KPBOT_UI_PASSWORD)", created.email)
    _audit_tenants()
    if not settings.is_production:
        return
    problems = settings.production_problems()
    if problems:
        raise RuntimeError(
            "нельзя запускаться в проде с такой конфигурацией:\n  - " + "\n  - ".join(problems)
        )
    log.info("конфигурация прода прошла проверку")


def _audit_tenants() -> None:
    """Кто обслуживается и всё ли на месте.

    Клиент без файла базы — это не «пустой клиент», а отказ в обслуживании:
    запросы к нему отклоняются, и владелец должен узнать об этом из журнала, а не
    от клиента.
    """
    tenants = registry.list()
    active = [t for t in tenants if t["status"] == "active"]
    log.info("клиентов в реестре: %s (активных %s)", len(tenants), len(active))
    for tenant in active:
        if not Path(tenant["db_path"]).exists():
            log.error(
                "база клиента %s (%s) не найдена — его запросы будут отклонены",
                tenant["id"],
                tenant["db_path"],
            )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Для reverse proxy и systemd: без авторизации, ничего чувствительного."""
    return {"status": "ok", "version": __version__}


# ---------- helpers ----------


RFQ_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Адрес получателя КП. Проверяем до сборки письма: перевод строки в заголовке
# To — это подстановка получателя (Bcc), а не «странный адрес».
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def ensure_rfq_id(rfq_id: str) -> str:
    """Идентификатор заявки — только 32 hex-символа, и только своя заявка.

    Без проверки значения из адреса попадает в путь к файлу (обход каталогов),
    а чужой id — в чужие документы: файлы КП лежат в общем каталоге, поэтому
    доступ проверяется по базе клиента, а не по имени файла.
    """
    if not RFQ_ID_RE.fullmatch(rfq_id or ""):
        raise HTTPException(status_code=404, detail="заявка не найдена")
    request = _CURRENT_REQUEST.get()
    if request is not None and request.url.path.startswith("/ui/"):
        page_path = f"/ui/rfq/{rfq_id}"
        # саму карточку отдаёт обработчик: он умеет показать «не найдено» страницей,
        # а не JSON-ошибкой
        if request.url.path.rstrip("/") != page_path:
            rfq = store.get_rfq(rfq_id)
            if rfq is None:
                raise HTTPException(status_code=404, detail="заявка не найдена")
            user = current_user(request)
            if (
                user is not None
                and not user.is_admin
                and rfq.get("assignee") not in (None, "", user.id)
            ):
                raise HTTPException(status_code=403, detail="заявка закреплена за другим менеджером")
    return rfq_id


def safe_int(value: object, default: int, low: int, high: int) -> int:
    """int из входных данных без падения на мусоре."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def rate_key(request: Request) -> str:
    """Лимит считаем по пользователю, иначе несколько менеджеров за одним IP делят квоту."""
    user = current_user(request)
    return f"user:{user.id}" if user else f"ip:{client_ip(request)}"


TENANT_COOKIE = "kpbot_tenant"
_tenant_db = supplier_db
# Текущий запрос: нужен там, где проверка доступа должна знать, кто пришёл
# (например, скачивание КП по id, а не по карточке).
_CURRENT_REQUEST = ContextVar("kpbot_current_request", default=None)


def _tenant_path_from_token(token: str | None, ttl: int) -> Path | None:
    if not token:
        return None
    tenant_id = user_from_session(settings.session_secret, token, ttl)
    if not tenant_id:
        return None
    tenant = registry.get(tenant_id)
    if not tenant or tenant["status"] != "active":
        return None
    path = Path(tenant["db_path"])
    return path if path.exists() else None


def tenant_db_from_request(request: Request) -> Path | None:
    """База клиента по подписанной куке админки; None — основная база."""
    return _tenant_path_from_token(request.cookies.get(TENANT_COOKIE), 86400 * 30)


def client_tenant_db_from_request(request: Request) -> Path | None:
    """База поставщика, в кабинет которого вошёл покупатель; None — основная."""
    return _tenant_path_from_token(request.cookies.get(CLIENT_TENANT_COOKIE), 86400 * 30)


def _api_key_ok(request: Request) -> bool:
    """Ключ API. Ключ клиента работает только со своим клиентом.

    Общий ключ владельца адресует только основную базу: иначе утечка одного ключа
    открывала бы заявки и финансы всех клиентов (аудит, дефект 23). Владельцу для
    работы в чужой базе нужен ключ этого клиента — он выдаётся в `/ui/ops`.
    """
    provided = request.headers.get("x-api-key") or ""
    if not provided:
        return False
    code = (request.headers.get("x-tenant") or "").strip()
    if code:
        tenant = registry.get(code)
        if not tenant or tenant["status"] != "active":
            return False
        return registry.verify_key(tenant["id"], provided)
    return constant_time_equal(provided, settings.api_key)


def request_tenant_path(request: Request) -> Path | None:
    """База для запроса: заголовок X-Tenant важнее куки.

    Раньше кука проверялась первой, поэтому ключ клиента B вместе с кукой
    клиента A исполнял запрос в базе A: проверка «ключ подходит к X-Tenant»
    становилась бесполезной. Для API клиента базу определяет ключ, а кука —
    это вход сотрудника в интерфейс, где X-Tenant не передаётся.
    """
    code = (request.headers.get("x-tenant") or "").strip()
    if code:
        if not _api_key_ok(request):
            return None
        tenant = registry.get(code)
        if not tenant or tenant["status"] != "active":
            return None
        path = Path(tenant["db_path"])
        return path if path.exists() else None
    return tenant_db_from_request(request)


def client_ip(request: Request) -> str:
    return resolve_client_ip(request, settings.trusted_proxy_set)


def session_of(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _changed_at_timestamp(value: str) -> float:
    """ISO-время смены пароля → epoch. Пустое/битое — 0 (проверка не нужна)."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def current_supplier_tenant(request: Request) -> str:
    """Код клиента, в чьей базе работает этот запрос (для привязки сессии)."""
    code = user_from_session(
        settings.session_secret, request.cookies.get(TENANT_COOKIE), 86400 * 30
    )
    if code and registry.get(code):
        return code
    header = (request.headers.get("x-tenant") or "").strip()
    if header and request_tenant_path(request) is not None and registry.get(header):
        return header
    return "default"


def _session_sub(tenant_id: str, user_id: str) -> str:
    """Кому выдан вход: клиент и человек.

    Без клиента в сессии вход одного поставщика принимался бы в базе другого:
    сессия — это просто подписанный id, и проверять его нужно в своём контексте.
    """
    return f"{tenant_id}:{user_id}"


def current_user(request: Request) -> User | None:
    """Кто пришёл: из подписанной куки. None — не авторизован.

    Cookie, выданная до смены пароля, недействительна: иначе уволенный
    сотрудник или украденный ноутбук работал бы до истечения TTL.
    """
    token = session_of(request)
    sub = user_from_session(settings.session_secret, token, settings.session_ttl_seconds)
    if not sub:
        return None
    tenant_id, _, user_id = sub.partition(":")
    if not user_id:
        # сессия старого формата (без клиента) — просим войти заново
        return None
    current = current_supplier_tenant(request)
    if tenant_id == current:
        user = store.user_by_id(user_id)
    elif tenant_id == "default":
        # Владелец сервиса: учётка в основной базе, он смотрит клиентов по /ui/ops.
        owner = Store(settings.db_path).user_by_id(user_id)
        user = owner if (owner is not None and owner.is_admin) else None
    else:
        user = None
    if user is None:
        return None
    changed = _changed_at_timestamp(user.password_changed_at)
    if changed:
        issued = session_issued_at(settings.session_secret, token, settings.session_ttl_seconds)
        if issued is None or issued + 1 < changed:
            return None
    return user


def is_authenticated(request: Request) -> bool:
    if current_user(request) is not None:
        return True
    if not settings.shared_login_allowed:
        # В проде вход общим паролем закрыт: нет ни личности, ни возможности
        # отозвать доступ у одного человека.
        return False
    # совместимость: одиночный пароль из конфигурации работает как вход администратора
    return verify_session(settings.session_secret, session_of(request), settings.session_ttl_seconds)


def actor_of(request: Request) -> str:
    """Кто действует: вошедший пользователь, иначе адрес (общий пароль/демо).

    В журнале должно быть видно человека, а не «ui:127.0.0.1», иначе на вопрос
    «кто одобрил эту цену» ответить нельзя.
    """
    user = current_user(request)
    if user is not None:
        return f"user:{user.email}"
    return f"ui:{client_ip(request)}"


def is_admin_request(request: Request) -> bool:
    """Администратор: пользователь с ролью admin или вход общим паролем."""
    user = current_user(request)
    if user is not None:
        return user.is_admin
    if not settings.shared_login_allowed:
        return False
    return verify_session(settings.session_secret, session_of(request), settings.session_ttl_seconds)


def page(name: str, request: Request, *, code: int = 200, **context: object) -> HTMLResponse:
    session = session_of(request)
    user = current_user(request)
    body = render_template(
        name,
        authenticated=is_authenticated(request),
        current_user=user,
        is_admin=bool(user and user.is_admin),
        is_owner=is_owner(request),
        tenant_name=_tenant_name(),
        csrf_token=csrf_for(settings.session_secret, session),
        price_count=store.price_count(),
        smtp_ready=bool(mailer.enabled and mailer.host),
        prices_include_vat=settings.prices_include_vat,
        shared_login_allowed=settings.shared_login_allowed,
        **context,
    )
    return HTMLResponse(body, status_code=code)


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------- UI ----------


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/ui")


def _upload_temp(suffix: str, prefix: str) -> Path:
    """Свой временный файл на каждый запрос.

    Раньше все клиенты писали в общий `price_upload.xlsx`: две одновременные
    загрузки разных поставщиков могли подменить файл друг друга — и прайс
    одного применялся в базе другого. Плюс файл оставался на диске навсегда.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / f"{prefix}-{uuid.uuid4().hex}{suffix}"


def _with_sender_names(rfqs: list[dict]) -> list[dict]:
    """В списке показываем компанию, а не «компания <почта>» — без дубля."""
    for rfq in rfqs:
        rfq["sender_name"] = sender_display_name(str(rfq.get("sender") or ""))
    return rfqs


@app.get("/ui", response_class=HTMLResponse)
def ui_inbox(request: Request, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    query = (request.query_params.get("q") or "").strip()[:100]
    status_filter = (request.query_params.get("status") or "").strip()[:30]
    page_number = safe_int(request.query_params.get("page"), 1, 1, 1000)
    per_page = 50
    # +1 строка, чтобы знать, есть ли следующая страница, не считая COUNT(*)
    rows = store.list_rfqs(
        per_page + 1,
        assignee=None if (user is None or user.is_admin) else user.id,
        query=query or None,
        status=status_filter or None,
        offset=(page_number - 1) * per_page,
    )
    has_next = len(rows) > per_page
    rfqs = _with_sender_names(rows[:per_page])
    return page(
        "inbox.html",
        request,
        active_page="inbox",
        rfqs=rfqs,
        users=store.users() if (user and user.is_admin) else [],
        report=store.report(30),
        flash=flash[:200],
        q=query,
        status_filter=status_filter,
        page_number=page_number,
        per_page=per_page,
        has_next=has_next,
    )


@app.get("/ui/login", response_class=HTMLResponse)
async def ui_login_form(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/ui", status_code=status.HTTP_303_SEE_OTHER)
    return page("login.html", request)


@app.post("/ui/login")
async def ui_login(request: Request):
    if not limiter.allow(f"login:{client_ip(request)}"):
        return page("login.html", request, code=429, error="слишком много попыток, подождите минуту")
    if not same_origin(request, settings.public_base_url, allow_opaque=True):
        log.warning(
            "вход с чужого Origin: %r (Sec-Fetch-Site: %r)",
            request.headers.get("origin"),
            request.headers.get("sec-fetch-site"),
        )
        return page("login.html", request, code=403, error="запрос пришёл с другого сайта")
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))

    user = store.authenticate(email, password) if email else None
    tenant_id: str | None = None
    if user is None and email:
        # логин может принадлежать другому клиенту: ищем по всем базам
        for tenant in registry.find_by_login(email):
            candidate = Store(Path(tenant["db_path"])).authenticate(email, password)
            if candidate is not None:
                user, tenant_id = candidate, tenant["id"]
                break
    if user is not None:
        # Сессия помнит, чьей базы этот человек: вход одного поставщика не должен
        # открывать админку другого даже при подменённой куке клиента.
        home = tenant_id or current_supplier_tenant(request)
        token = make_user_session(
            settings.session_secret, _session_sub(home, user.id), settings.session_ttl_seconds
        )
    elif not email and constant_time_equal(password, settings.ui_password):
        if not settings.shared_login_allowed:
            return page(
                "login.html", request, code=403,
                error="нужен логин: введите почту пользователя (например admin@local) "
                      "и пароль. Вход одним паролем отключён",
            )
        # вход администратора общим паролем (обратная совместимость)
        token = make_session(settings.session_secret, settings.session_ttl_seconds)
    else:
        log.warning("неудачный вход с %s (%s)", client_ip(request), email or "без логина")
        return page("login.html", request, code=401, error="неверный логин или пароль")

    response = RedirectResponse("/ui", status_code=status.HTTP_303_SEE_OTHER)
    if tenant_id:
        response.set_cookie(
            TENANT_COOKIE,
            make_user_session(settings.session_secret, tenant_id, 86400 * 30),
            httponly=True,
            samesite="lax",
            secure=settings.public_base_url.lower().startswith("https"),
            max_age=86400 * 30,
            path="/",
        )
    else:
        # Вход без клиента не должен унаследовать базу предыдущего клиента с этой машины.
        response.delete_cookie(TENANT_COOKIE, path="/")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.lower().startswith("https"),
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    return response


@app.post("/ui/logout")
async def ui_logout(request: Request):
    if not is_authenticated(request):
        return login_redirect(request)
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(TENANT_COOKIE, path="/")
    return response


@app.post("/ui/upload")
async def ui_upload(request: Request, file: UploadFile):
    if not is_authenticated(request):
        return login_redirect(request)
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    if not limiter.allow(f"upload:{rate_key(request)}"):
        return PlainTextResponse("слишком много загрузок", status_code=429)

    filename = (file.filename or "").lower()
    if not filename.endswith((".eml", ".txt")):
        return page("inbox.html", request, code=400, rfqs=store.list_rfqs(100),
                    error="поддерживаются только .eml и .txt")

    raw = await file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        return page("inbox.html", request, code=413, rfqs=store.list_rfqs(100),
                    error=_file_too_big())
    try:
        result = await run_in_threadpool(
            pipeline.process_eml_bytes, raw, actor=actor_of(request)
        )
    except NotAnRfq:
        return page("inbox.html", request, code=422, rfqs=store.list_rfqs(100),
                    error="в письме не найдено ни одной позиции с количеством — это не заявка")
    except ValueError as exc:
        return page("inbox.html", request, code=400, rfqs=store.list_rfqs(100), error=str(exc))

    target = f"/ui/rfq/{result.rfq_id}"
    flash = "письмо уже было обработано" if result.duplicate else "письмо разобрано"
    return RedirectResponse(f"{target}?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


async def apply_company_form(form) -> str:
    """Реквизиты, цвет и логотип — общая логика для настроек и визарда.

    Возвращает предупреждение для человека (пустая строка — всё в порядке).
    """
    allowed = (
        "name", "inn", "kpp", "address", "phone", "email", "signer", "signature", "kp_intro",
        "account", "bank", "bik", "corr_account",
    )
    values = {f"company_{key}": str(form.get(key, "")).strip()[:500] for key in allowed}
    vat_warning = ""

    # Расчёт клиента: ставка, режим цен и доставка. Пишем только если поля пришли —
    # мастер подключения эти поля не отправляет и не должен сбрасывать настройки.
    # Пустое поле означает «как на сервере»: сохраняем пусто, расчёт возьмёт .env.
    if "vat_rate" in form:
        raw_rate = str(form.get("vat_rate", "")).strip().replace(",", ".")
        if not raw_rate:
            values["vat_rate"] = ""
        else:
            try:
                percent = Decimal(raw_rate)
                if 0 <= percent <= 100:
                    rate = (percent / 100).quantize(Decimal("0.0001"))
                    if rate in LEGAL_VAT_RATES:
                        values["vat_rate"] = str(rate)
                    else:
                        # 20% и 18% — ставки прошлых лет. С 2026 года основная
                        # 22% (ст. 164 НК РФ): сохранять незаконную ставку нельзя.
                        vat_warning = (
                            f"ставка НДС {percent:g}% не применяется с 01.01.2026 — "
                            "оставлена прежняя. Законные ставки: 22% (основная), 10%, 7%, 5%, 0%"
                        )
                        log.error("настройки: %s", vat_warning)
                else:
                    log.warning("ставка НДС в форме вне 0..100: %r — оставляю прежнюю", raw_rate)
            except (InvalidOperation, ValueError):
                log.warning("ставка НДС в форме не число: %r — оставляю прежнюю", raw_rate)
    if "delivery_price" in form:
        raw_delivery = str(form.get("delivery_price", "")).strip().replace(",", ".")
        if not raw_delivery:
            values["delivery_price"] = ""
        else:
            try:
                delivery = Decimal(raw_delivery)
                if delivery >= 0:
                    values["delivery_price"] = str(delivery.quantize(Decimal("0.01")))
            except (InvalidOperation, ValueError):
                log.warning("доставка в форме не число: %r — оставляю прежнюю", raw_delivery)
    if "prices_include_vat" in form or "vat_mode" in form:
        values["prices_include_vat"] = "1" if str(form.get("prices_include_vat", "")) else "0"

    color = str(form.get("color", "")).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        values["company_color"] = color.lower()

    logo = form.get("logo")
    if isinstance(logo, StarletteUploadFile) and logo.filename:
        raw = await logo.read(settings.max_logo_bytes + 1)
        if len(raw) > settings.max_logo_bytes:
            log.warning("логотип больше %s байт — пропущен", settings.max_logo_bytes)
        else:
            kind = (logo.content_type or "").split("/")[-1].lower()
            if kind in ("png", "jpeg", "jpg", "webp", "svg+xml"):
                values["company_logo"] = "data:image/%s;base64,%s" % (kind, base64.b64encode(raw).decode("ascii"))

    store.set_settings(values)
    return vat_warning


@app.get("/ui/stats", response_class=HTMLResponse)
def ui_stats(request: Request, days: str = "30"):
    """Сводка живёт на главной: отдельная страница больше не нужна."""
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    rfqs = _with_sender_names(
        store.list_rfqs(100, assignee=None if (user is None or user.is_admin) else user.id)
    )
    return page(
        "inbox.html",
        request,
        active_page="inbox",
        rfqs=rfqs,
        users=store.users() if (user and user.is_admin) else [],
        report=store.report(safe_int(days, 30, 1, 365)),
    )


@app.get("/ui/report", response_class=HTMLResponse)
def ui_pilot_report(request: Request, days: str = "30"):
    """Отчёт пилота: цифры для клиента, без текстов писем и персональных данных."""
    if not is_authenticated(request):
        return login_redirect(request)
    period = safe_int(days, 30, 1, 365)
    company = store.get_settings().get("company_name") or settings.company_name
    user = current_user(request)
    # Менеджер видит отчёт только по своим заявкам: в таблице «что не нашли»
    # выводятся строки из писем, а это переписка клиентов.
    assignee = None if (user is None or user.is_admin) else user.id
    return HTMLResponse(
        build_pilot_report(store, days=period, company=company, assignee=assignee)
    )


@app.get("/ui/prices", response_class=HTMLResponse)
def ui_prices(request: Request, q: str = "", flash: str = ""):
    """Прайс: сколько позиций, когда обновляли, поиск и загрузка файла."""
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    items = store.price_items()
    # «ё» и «е» — одна буква для поиска: иначе «елка» не находит «Ёлку».
    query = q.strip().lower().replace("ё", "е")
    if query:
        items = [
            item
            for item in items
            if query in item.name.lower().replace("ё", "е") or query in item.sku.lower()
        ]
    # Ставка строки: своя из прайса или ставка клиента. Показываем действующую —
    # иначе при смешанных 22/10 менеджер не видит, чем отличается позиция.
    default_rate = pipeline.calc().vat_rate
    for index, item in enumerate(items):
        if item.vat_rate is None:
            items[index] = replace(item, vat_rate=default_rate)
    page_size = 300
    shown_items = items[:page_size]
    return page(
        "prices.html",
        request,
        active_page="prices",
        items=shown_items,
        stock=store.stock_map(),
        stock_count=store.stock_count(),
        stock_updated_at=store.stock_updated_at(),
        total=store.price_count(),
        matched=len(items),
        shown=len(shown_items),
        truncated=len(items) > page_size,
        query=q.strip(),
        updated_at=store.price_updated_at(),
        can_edit=is_admin_request(request),
        one_c_configured=bool(settings.one_c_base_url),
        flash=flash[:200],
    )


@app.post("/ui/prices/upload")
async def ui_prices_upload(request: Request):
    """Загрузка прайса из файла 1С/Excel: заменяет весь список."""
    if not is_authenticated(request):
        return login_redirect(request)
    if not is_admin_request(request):
        return PlainTextResponse("прайс меняет только администратор", status_code=403)

    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    if not limiter.allow(f"upload:{rate_key(request)}"):
        return PlainTextResponse("слишком много загрузок", status_code=429)

    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile):
        return RedirectResponse("/ui/prices?flash=файл+не+выбран", status_code=status.HTTP_303_SEE_OTHER)

    filename = (upload.filename or "").lower()
    if not filename.endswith((".xlsx", ".csv")):
        return RedirectResponse(
            f"/ui/prices?flash={quote('поддерживаются .xlsx и .csv')}", status_code=status.HTTP_303_SEE_OTHER
        )

    raw = await upload.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        return RedirectResponse(
            f"/ui/prices?flash={quote(_file_too_big())}", status_code=status.HTTP_303_SEE_OTHER
        )

    suffix = ".xlsx" if filename.endswith(".xlsx") else ".csv"
    temp_path = _upload_temp(suffix, "price")
    temp_path.write_bytes(raw)
    try:
        items = load_price_items(
            temp_path, prices_include_vat=pipeline.calc().prices_include_vat
        )
    except ValidationError as exc:
        temp_path.unlink(missing_ok=True)
        return RedirectResponse(f"/ui/prices?flash={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:  # noqa: BLE001 - файл клиента может быть любым
        temp_path.unlink(missing_ok=True)
        log.warning("прайс не разобран: %s", exc)
        return RedirectResponse(
            f"/ui/prices?flash={quote('файл не разобран: ' + str(exc)[:120])}", status_code=status.HTTP_303_SEE_OTHER
        )

    count = store.replace_price_items(items)
    temp_path.unlink(missing_ok=True)
    store.log_event("price_upload", actor_of(request), f"позиций {count}, файл {filename}")
    flash = f"прайс обновлён: {count} позиций"
    return RedirectResponse(f"/ui/prices?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


# ---------- админка ----------


def admin_only(request: Request):
    """Пропускает только администратора; иначе — на вход."""
    if not is_authenticated(request):
        return login_redirect(request)
    if not is_admin_request(request):
        return PlainTextResponse("раздел только для администратора", status_code=403)
    return None


@app.get("/ui/settings", response_class=HTMLResponse)
async def ui_settings(request: Request, flash: str = ""):
    """Реквизиты компании — вкладка в админке, отдельной страницы нет."""
    return await render_admin(request, tab="company", flash=flash)


@app.post("/ui/settings")
async def ui_settings_save(request: Request):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    warning = await apply_company_form(form)
    store.log_event("company_settings", actor_of(request), str(form.get("name", ""))[:120])
    return RedirectResponse(
        f"/ui/settings?flash={quote(warning or 'настройки сохранены')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

def setup_state() -> dict:
    """Три условия готовности: реквизиты, прайс, почта."""
    company = pipeline.company()
    saved = store.get_settings()
    price = store.price_count()
    mail_ready = bool(mailer.enabled and mailer.host)
    company_ready = bool(saved.get("company_name"))
    return {
        "company_ready": company_ready,
        "price_count": price,
        "mail_ready": mail_ready,
        "done": company_ready and price > 0 and mail_ready,
        "company": company,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "imap_user": settings.imap_user,
        "imap_folder": settings.imap_folder,
    }


@app.get("/ui/setup", response_class=HTMLResponse)
async def ui_setup(request: Request, flash: str = ""):
    denied = admin_only(request)
    if denied is not None:
        return denied
    return page("setup.html", request, active_page="setup", state=setup_state(), flash=flash[:200])


@app.post("/ui/setup/company")
async def ui_setup_company(request: Request):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    warning = await apply_company_form(form)
    store.log_event("setup_company", actor_of(request), str(form.get("name", ""))[:120])
    return RedirectResponse(
        f"/ui/setup?flash={quote(warning or 'шаг 1 готов: реквизиты сохранены')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/setup/price")
async def ui_setup_price(request: Request):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile):
        return RedirectResponse(
            f"/ui/setup?flash={quote('файл не выбран')}", status_code=status.HTTP_303_SEE_OTHER
        )
    filename = (upload.filename or "").lower()
    if not filename.endswith((".xlsx", ".csv")):
        return RedirectResponse(
            f"/ui/setup?flash={quote('нужен .xlsx или .csv')}", status_code=status.HTTP_303_SEE_OTHER
        )
    raw = await upload.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        return RedirectResponse(
            f"/ui/setup?flash={quote(_file_too_big())}", status_code=status.HTTP_303_SEE_OTHER
        )
    suffix = ".xlsx" if filename.endswith(".xlsx") else ".csv"
    temp_path = _upload_temp(suffix, "price")
    temp_path.write_bytes(raw)
    try:
        items = load_price_items(
            temp_path, prices_include_vat=pipeline.calc().prices_include_vat
        )
    except ValidationError as exc:
        temp_path.unlink(missing_ok=True)
        return RedirectResponse(f"/ui/setup?flash={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:  # noqa: BLE001 - файл клиента может быть любым
        temp_path.unlink(missing_ok=True)
        log.warning("прайс в визарде не разобран: %s", exc)
        return RedirectResponse(
            f"/ui/setup?flash={quote('файл не разобран: ' + str(exc)[:120])}", status_code=status.HTTP_303_SEE_OTHER
        )
    count = store.replace_price_items(items)
    temp_path.unlink(missing_ok=True)
    store.log_event("setup_price", actor_of(request), f"позиций {count}, файл {filename}")
    return RedirectResponse(
        f"/ui/setup?flash={quote(f'шаг 2 готов: прайс {count} позиций')}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/ui/setup/mail")
async def ui_setup_mail(request: Request):
    """Проверяет почтовые настройки, ничего не меняя."""
    denied = admin_only(request)
    if denied is not None:
        return denied
    await verified_form(request)
    from .mailer import MailError, check_connection

    try:
        message = check_connection(settings)
    except MailError as exc:
        return RedirectResponse(
            f"/ui/setup?flash={quote('почта: ' + str(exc)[:160])}", status_code=status.HTTP_303_SEE_OTHER
        )
    store.log_event("setup_mail", actor_of(request), message[:160])
    return RedirectResponse(
        f"/ui/setup?flash={quote('шаг 3 готов: ' + message[:140])}", status_code=status.HTTP_303_SEE_OTHER
    )


def admin_context(request: Request) -> dict:
    """Всё, что нужно любой вкладке админки: одна выборка, один рендер."""
    db_path = settings.db_path
    return {
        "users": store.users(),
        "events": store.list_events(100),
        "pairs": store.learned_pairs(300),
        "learned_total": store.learned_count(),
        "company": pipeline.company(),
        "calc": pipeline.calc(),
        "totals": store.stats(),
        "report": store.report(30),
        "diagnostics": {
            "llm": bool(settings.llm_enabled and settings.llm_base_url),
            "llm_model": settings.llm_model or "—",
            "smtp": bool(mailer.enabled and mailer.host),
            "telegram": bool(settings.telegram_enabled and settings.telegram_bot_token),
            "max": bool(settings.max_enabled and settings.max_bot_token),
            "imap": settings.imap_user or "—",
            "imap_folder": settings.imap_folder,
            "pdf": settings.pdf_enabled,
            "env": "боевой" if settings.is_production else "разработка",
            "db_mb": round(db_path.stat().st_size / 1024 / 1024, 2) if db_path.exists() else 0,
        },
    }


async def render_admin(request: Request, *, tab: str = "state", flash: str = ""):
    denied = admin_only(request)
    if denied is not None:
        return denied
    allowed = ("state", "users", "learned", "events", "company", "clients", "orders")
    tab = tab if tab in allowed else "state"
    context = admin_context(request)
    if tab == "clients":
        cabinet = CabinetStore(current_supplier_db(settings.db_path))
        companies = cabinet.list_companies()
        for company in companies:
            company["users"] = cabinet.list_users(company["id"])
        context["clients"] = companies
    elif tab == "orders":
        context["client_orders"] = CabinetStore(current_supplier_db(settings.db_path)).all_orders()
    context["order_statuses"] = ORDER_STATUSES
    context["order_labels"] = ORDER_STATUS_LABELS
    return page(
        "admin.html",
        request,
        active_page="admin",
        tab=tab,
        flash=flash[:200],
        **context,
    )


def _tenant_name() -> str:
    """Название текущего клиента — для шапки, чтобы не перепутать базы."""
    current = _tenant_db.get()
    if current is None:
        return ""
    for tenant in registry.list():
        if Path(tenant["db_path"]) == Path(current):
            return tenant["name"]
    return ""


def is_owner(request: Request) -> bool:
    """Владелец сервиса: администратор основной базы (не клиентской)."""
    if not is_admin_request(request):
        return False
    current = _tenant_db.get()
    return current is None or current == settings.db_path


def _send_status_emails(recipients: list[str], subject: str, body: str) -> int:
    """Письма клиенту о статусе заказа. Сбой одного адреса не мешает остальным."""
    from email.message import EmailMessage

    sent = 0
    for address in recipients:
        message = EmailMessage()
        message["From"] = mailer.from_addr or settings.smtp_user or "no-reply@localhost"
        message["To"] = address
        message["Subject"] = subject
        message.set_content(body)
        try:
            mailer.send(message, to=address)
            sent += 1
        except MailError:
            log.warning("не удалось отправить уведомление клиенту %s", address)
    return sent


@app.get("/ui/ops", response_class=HTMLResponse)
def ui_ops(request: Request, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    tenants = registry.list()
    keyed = {tenant["id"]: registry.has_key(tenant["id"]) for tenant in tenants}
    return page(
        "ops.html",
        request,
        active_page="ops",
        tenants=tenants,
        keyed=keyed,
        flash=flash[:300],
    )


@app.post("/ui/ops/create")
async def ui_ops_create(request: Request):
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    form = await verified_form(request)
    name = str(form.get("name", "")).strip()[:120]
    email = normalize_email(str(form.get("email", "")))
    if not name or "@" not in email:
        return RedirectResponse(
            f"/ui/ops?flash={quote('нужны название и почта администратора')}", status_code=status.HTTP_303_SEE_OTHER
        )

    tenant = registry.create(name, settings.tenants_dir / f"{uuid.uuid4().hex[:12]}.sqlite3")
    path = Path(tenant["db_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tenant_store = Store(path)
    tenant_store.init_schema()

    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    try:
        tenant_store.create_user(email=email, password=password, role=ROLE_ADMIN, name="Администратор")
    except Exception as exc:  # noqa: BLE001 - показываем причину владельцу
        registry.set_status(tenant["id"], "paused")
        return RedirectResponse(
            f"/ui/ops?flash={quote('клиент создан, но пользователь не заведён: ' + str(exc)[:100])}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    store.log_event("tenant_created", actor_of(request), f"{name} · {tenant['id']} · {email}")
    flash = f"клиент «{name}»: вход {email} · пароль {password} (покажется один раз)"
    # Отвечаем страницей, а не редиректом: пароль не должен попадать в адресную
    # строку, историю браузера и логи reverse-proxy.
    return page("ops.html", request, active_page="ops", tenants=registry.list(), flash=flash)


@app.post("/ui/ops/switch")
async def ui_ops_switch(request: Request):
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    form = await verified_form(request)
    tenant_id = str(form.get("tenant_id", "")).strip()
    tenant = registry.get(tenant_id)
    if tenant is None:
        return RedirectResponse(f"/ui/ops?flash={quote('клиент не найден')}", status_code=status.HTTP_303_SEE_OTHER)

    response = RedirectResponse("/ui", status_code=status.HTTP_303_SEE_OTHER)
    if tenant["id"] == registry.ensure_default():
        response.delete_cookie(TENANT_COOKIE, path="/")
    else:
        response.set_cookie(
            TENANT_COOKIE,
            make_user_session(settings.session_secret, tenant["id"], 86400 * 30),
            httponly=True,
            samesite="lax",
            secure=settings.public_base_url.lower().startswith("https"),
            max_age=86400 * 30,
            path="/",
        )
    store.log_event("tenant_switch", actor_of(request), tenant["id"])
    return response


@app.post("/ui/ops/status")
async def ui_ops_status(request: Request):
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    form = await verified_form(request)
    tenant_id = str(form.get("tenant_id", "")).strip()
    status_value = str(form.get("status", "active")).strip()
    registry.set_status(tenant_id, status_value)
    store.log_event("tenant_status", actor_of(request), f"{tenant_id} → {status_value}")
    return RedirectResponse(f"/ui/ops?flash={quote('статус обновлён')}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/ui/ops/key")
async def ui_ops_key(request: Request):
    """Выпуск нового ключа API клиенту. Ключ показывается один раз."""
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    form = await verified_form(request)
    tenant_id = str(form.get("tenant_id", "")).strip()
    tenant = registry.get(tenant_id)
    if tenant is None:
        return RedirectResponse(f"/ui/ops?flash={quote('клиент не найден')}", status_code=status.HTTP_303_SEE_OTHER)
    key = registry.issue_key(tenant["id"])
    store.log_event("tenant_key", actor_of(request), tenant["id"])
    tenants = registry.list()
    keyed = {item["id"]: registry.has_key(item["id"]) for item in tenants}
    return page(
        "ops.html",
        request,
        active_page="ops",
        tenants=tenants,
        keyed=keyed,
        flash=f"Ключ API для «{tenant['name']}»: {key} — сохраните, больше не покажется",
        new_key=key,
    )


@app.get("/ui/admin", response_class=HTMLResponse)
async def ui_admin(request: Request, tab: str = "state", flash: str = ""):
    return await render_admin(request, tab=tab, flash=flash)


@app.get("/ui/admin/users", response_class=HTMLResponse)
async def ui_admin_users(request: Request, flash: str = ""):
    return await render_admin(request, tab="users", flash=flash)


@app.post("/ui/admin/users")
async def ui_admin_user_create(request: Request):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    role = str(form.get("role", "manager"))
    name = str(form.get("name", "")).strip()
    try:
        user = store.create_user(email=email, password=password, role=role, name=name)
    except Exception as exc:  # noqa: BLE001 - причину показываем человеку
        return RedirectResponse(f"/ui/admin/users?flash={quote(str(exc)[:160])}", status_code=status.HTTP_303_SEE_OTHER)
    store.log_event("user_created", actor_of(request), f"{user.email} · {user.role}")
    return RedirectResponse(
        f"/ui/admin/users?flash={quote('создан ' + user.email)}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/ui/admin/users/{user_id}/role")
async def ui_admin_user_role(request: Request, user_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    role = str(form.get("role", "manager"))
    current = store.user_by_id(user_id)
    if current is None:
        return RedirectResponse(f"/ui/admin/users?flash={quote('пользователь не найден')}", status_code=303)
    if current.role == ROLE_ADMIN and role != ROLE_ADMIN and store.admin_count() <= 1:
        return RedirectResponse(
            f"/ui/admin/users?flash={quote('нельзя снять роль с последнего администратора')}", status_code=303
        )
    store.set_user_role(user_id, role)
    store.log_event("user_role", actor_of(request), f"{current.email} → {role}")
    return RedirectResponse(f"/ui/admin/users?flash={quote('роль обновлена')}", status_code=303)


@app.post("/ui/admin/users/{user_id}/password")
async def ui_admin_user_password(request: Request, user_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    password = str(form.get("password", ""))
    current = store.user_by_id(user_id)
    if current is None:
        return RedirectResponse(f"/ui/admin/users?flash={quote('пользователь не найден')}", status_code=303)
    try:
        store.set_user_password(user_id, password)
    except ValueError as exc:
        return RedirectResponse(f"/ui/admin/users?flash={quote(str(exc))}", status_code=303)
    store.log_event("user_password", actor_of(request), current.email)
    return RedirectResponse(f"/ui/admin/users?flash={quote('пароль обновлён')}", status_code=303)


@app.post("/ui/admin/users/{user_id}/delete")
async def ui_admin_user_delete(request: Request, user_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    await verified_form(request)
    current = store.user_by_id(user_id)
    if current is None:
        return RedirectResponse(f"/ui/admin/users?flash={quote('пользователь не найден')}", status_code=303)
    me = current_user(request)
    if me is not None and me.id == user_id:
        return RedirectResponse(
            f"/ui/admin/users?flash={quote('нельзя заблокировать себя')}", status_code=303
        )
    if current.role == ROLE_ADMIN and store.admin_count() <= 1:
        return RedirectResponse(
            f"/ui/admin/users?flash={quote('нельзя удалить последнего администратора')}", status_code=303
        )
    store.delete_user(user_id)
    store.log_event("user_blocked", actor_of(request), current.email)
    return RedirectResponse(
        f"/ui/admin/users?flash={quote('пользователь заблокирован, история сохранена')}", status_code=303
    )


# ---------- ЛК клиента: управление из админки поставщика ----------


@app.post("/ui/admin/clients")
async def ui_admin_client_create(request: Request):
    """Поставщик создаёт компанию-покупателя и первого пользователя ЛК."""
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    name = str(form.get("name", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    role = str(form.get("role", "client_admin"))
    if not name or "@" not in email:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote('нужны название компании и e-mail')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    password = str(form.get("password", "")).strip() or secrets.token_urlsafe(9)
    cabinet = CabinetStore(current_supplier_db(settings.db_path))
    try:
        company = cabinet.create_company(
            name, inn=str(form.get("inn", "")).strip(), kpp=str(form.get("kpp", "")).strip()
        )
        cabinet.create_user(company["id"], email=email, password=password, role=role,
                            name=str(form.get("user_name", "")).strip())
    except (ValueError, sqlite3.IntegrityError) as exc:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote(str(exc)[:160])}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    store.log_event("client_created", actor_of(request), f"{name} / {email}")
    message = f"клиент {name} создан. Вход: {email}, пароль: {password} — покажите один раз"
    # Пароль — в теле ответа, а не в адресе: иначе он остаётся в логах прокси.
    return await render_admin(request, tab="clients", flash=message[:300])


@app.post("/ui/admin/clients/{company_id}/status")
async def ui_admin_client_status(request: Request, company_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    new_status = str(form.get("status", "active"))
    cabinet = CabinetStore(current_supplier_db(settings.db_path))
    if cabinet.company(company_id) is None:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote('компания не найдена')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    cabinet.set_company_status(company_id, new_status)
    store.log_event("client_status", actor_of(request), f"{company_id}: {new_status}")
    return RedirectResponse(
        f"/ui/admin?tab=clients&flash={quote('статус компании обновлён')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/admin/clients/{company_id}/users")
async def ui_admin_client_user(request: Request, company_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", "")).strip() or secrets.token_urlsafe(9)
    role = str(form.get("role", "client_manager"))
    cabinet = CabinetStore(current_supplier_db(settings.db_path))
    if cabinet.company(company_id) is None:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote('компания не найдена')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        cabinet.create_user(company_id, email=email, password=password, role=role,
                            name=str(form.get("name", "")).strip())
    except (ValueError, sqlite3.IntegrityError) as exc:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote(str(exc)[:160])}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    store.log_event("client_user_created", actor_of(request), email)
    message = f"пользователь {email} создан, пароль: {password} — покажите один раз"
    return await render_admin(request, tab="clients", flash=message[:300])


@app.post("/ui/admin/clients/{company_id}/users/{user_id}/password")
async def ui_admin_client_user_password(request: Request, company_id: str, user_id: str):
    """Сброс пароля клиентского пользователя: новый показывается один раз."""
    denied = admin_only(request)
    if denied is not None:
        return denied
    await verified_form(request)
    cabinet = CabinetStore(current_supplier_db(settings.db_path))
    user = cabinet.user_by_id(user_id)
    if user is None or user["company_id"] != company_id:
        return RedirectResponse(
            f"/ui/admin?tab=clients&flash={quote('пользователь не найден')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    password = secrets.token_urlsafe(9)
    cabinet.set_password(user_id, password)
    store.log_event("client_password_reset", actor_of(request), user["email"])
    message = f"новый пароль для {user['email']}: {password} — покажите один раз"
    return await render_admin(request, tab="clients", flash=message[:300])


@app.post("/ui/admin/orders/{order_id}/status")
async def ui_admin_order_status(request: Request, order_id: str):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    new_status = str(form.get("status", "pending"))
    cabinet = CabinetStore(current_supplier_db(settings.db_path))
    order = cabinet.order(order_id)
    if order is None:
        return RedirectResponse(
            f"/ui/admin?tab=orders&flash={quote('заказ не найден')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    transition_error = order_transition_error(order["status"], new_status)
    if transition_error:
        return RedirectResponse(
            f"/ui/admin?tab=orders&flash={quote(transition_error)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    cabinet.update_status(order_id, new_status)
    cabinet.log_order_event(order_id, order["company_id"], new_status)
    store.log_event("client_order_status", actor_of(request), f"{order_id}: {new_status}")

    # Клиент не должен узнавать о смене статуса только зайдя в кабинет.
    if settings.cabinet_notify_email and mailer.enabled and mailer.host:
        recipients = [user["email"] for user in cabinet.list_users(order["company_id"])]
        if recipients:
            label = ORDER_STATUS_LABELS.get(new_status, new_status)
            link = f"{settings.public_base_url.rstrip('/')}/cabinet/orders/{order_id}"
            subject = f"Заказ {order['number']}: {label}"
            body = (
                f"Здравствуйте!\n\n"
                f"Статус вашего заказа {order['number']} изменился: {label}.\n"
                f"Открыть заказ: {link}\n\n"
                f"— {pipeline.company().get('name') or settings.company_name}"
            )
            try:
                sent = await run_in_threadpool(_send_status_emails, recipients, subject, body)
                if sent:
                    store.log_event(
                        "client_order_notified", actor_of(request), f"{order['number']}: писем {sent}"
                    )
            except Exception as exc:  # noqa: BLE001 - уведомление не должно ломать действие
                log.warning("уведомление клиента не отправлено: %s", exc)

    return RedirectResponse(
        f"/ui/admin?tab=orders&flash={quote('статус заказа обновлён')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/ui/admin/learned", response_class=HTMLResponse)
async def ui_admin_learned(request: Request, flash: str = ""):
    return await render_admin(request, tab="learned", flash=flash)


@app.post("/ui/admin/learned/forget")
async def ui_admin_learned_forget(request: Request):
    denied = admin_only(request)
    if denied is not None:
        return denied
    form = await verified_form(request)
    key = str(form.get("key", "")).strip()
    removed = store.forget_learned(key or None)
    store.log_event("learned_forget", actor_of(request), key or "всё обучение")
    flash = f"удалено пар: {removed}"
    return RedirectResponse(f"/ui/admin/learned?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/admin/events", response_class=HTMLResponse)
async def ui_admin_events(request: Request):
    return await render_admin(request, tab="events")


@app.get("/ui/prices/search")
async def ui_prices_search(request: Request, q: str = "", limit: int = 20):
    """Подсказка по прайсу для карточки: вместо 10 000 <option> в каждой строке."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="нужен вход")
    query = (q or "").strip()[:80]
    if len(query) < 2:
        return JSONResponse([])
    size = safe_int(limit, 20, 1, 50)
    index = pipeline.price_index()
    return JSONResponse(
        [
            {"sku": item.sku, "name": item.name, "unit": item.unit, "price": str(item.price)}
            for item, _score in index.top(query, size)
        ]
    )


@app.post("/ui/prices/stock")
async def ui_prices_stock(request: Request):
    """Загрузка остатков из выгрузки 1С: подсказывает, чего не хватает."""
    if not is_authenticated(request):
        return login_redirect(request)
    if not is_admin_request(request):
        return PlainTextResponse("остатки загружает только администратор", status_code=403)

    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)

    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        return RedirectResponse("/ui/prices?flash=файл+не+выбран", status_code=status.HTTP_303_SEE_OTHER)

    filename = (upload.filename or "").lower()
    if not filename.endswith((".xlsx", ".csv")):
        return RedirectResponse(
            f"/ui/prices?flash={quote('нужен .xlsx или .csv')}", status_code=status.HTTP_303_SEE_OTHER
        )
    raw = await upload.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        return RedirectResponse(
            f"/ui/prices?flash={quote(_file_too_big())}", status_code=status.HTTP_303_SEE_OTHER
        )

    suffix = ".xlsx" if filename.endswith(".xlsx") else ".csv"
    temp_path = _upload_temp(suffix, "stock")
    temp_path.write_bytes(raw)
    try:
        items = load_stock_items(temp_path)
    except ValidationError as exc:
        temp_path.unlink(missing_ok=True)
        return RedirectResponse(f"/ui/prices?flash={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER)

    count = store.replace_stock(items)
    temp_path.unlink(missing_ok=True)
    store.log_event("stock_upload", actor_of(request), f"позиций {count}, файл {filename}")
    return RedirectResponse(
        f"/ui/prices?flash={quote(f'остатки обновлены: {count} позиций')}", status_code=status.HTTP_303_SEE_OTHER
    )


def _file_too_big() -> str:
    """Понятный текст про лимит: он настраивается, «2 МБ» — только значение по умолчанию."""
    megabytes = settings.max_upload_bytes / (1024 * 1024)
    return f"файл больше {megabytes:g} МБ — уменьшите файл"


def _prices_flash(message: str) -> RedirectResponse:
    return RedirectResponse(
        f"/ui/prices?flash={quote(message[:200])}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/ui/prices/from-1c")
async def ui_prices_from_1c(request: Request):
    """Цены из 1С по OData — без ручной выгрузки файла."""
    denied = admin_only(request)
    if denied is not None:
        return denied
    await verified_form(request)
    if not settings.one_c_base_url:
        return _prices_flash("1С не настроена: заполните KPBOT_ONE_C_* в .env")
    try:
        incoming = await run_in_threadpool(lambda: client_from_settings(settings).prices())
    except OneCError as exc:
        return _prices_flash(f"1С: {exc}")
    items, changed = merge_prices(store.price_items(), incoming)
    count = store.replace_price_items(items)
    store.log_event("price_from_1c", actor_of(request), f"изменено {changed}, всего {count}")
    return _prices_flash(f"цены из 1С: {len(incoming)} позиций, изменено {changed}, всего {count}")


@app.post("/ui/prices/stock-1c")
async def ui_prices_stock_1c(request: Request):
    """Остатки из 1С по OData: регистр или документы."""
    denied = admin_only(request)
    if denied is not None:
        return denied
    await verified_form(request)
    if not settings.one_c_base_url:
        return _prices_flash("1С не настроена: заполните KPBOT_ONE_C_* в .env")
    try:
        rows = await run_in_threadpool(lambda: client_from_settings(settings).stock_auto())
    except OneCError as exc:
        return _prices_flash(f"1С: {exc}")
    count = store.replace_stock(rows)
    store.log_event("stock_from_1c", actor_of(request), f"позиций {count}")
    return _prices_flash(f"остатки из 1С: {count} позиций")


def stock_for_lines(lines: list[dict], stock: dict, items) -> dict[int, dict]:
    """Остаток по строке заявки: сначала по артикулу, потом по наименованию.

    Отчёты 1С часто идут без артикула — тогда ключом в файле остатков
    становится наименование, а в заявке оно уже сопоставлено с прайсом.
    """
    by_name = {item.name.strip().lower(): item.sku for item in items}
    result: dict[int, dict] = {}
    for line in lines:
        sku = line.get("matched_sku")
        entry = stock.get(sku) if sku else None
        if entry is None:
            name = (line.get("matched_name") or "").strip().lower()
            if name:
                entry = stock.get(by_name.get(name, "")) or stock.get(name)
        result[line["idx"]] = entry or {}
    return result


@app.get("/ui/rfq/{rfq_id}", response_class=HTMLResponse)
def ui_rfq(request: Request, rfq_id: str, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    user = current_user(request)
    is_admin_request = bool(user and user.is_admin)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        return page("inbox.html", request, code=404, rfqs=store.list_rfqs(100), error="заявка не найдена")
    if not is_admin_request and user is not None and rfq.get("assignee") not in (None, "", user.id):
        return page(
            "inbox.html",
            request,
            code=403,
            rfqs=store.list_rfqs(100, assignee=user.id),
            error="заявка закреплена за другим менеджером",
        )
    quote = store.get_quote(rfq_id)
    reply_text = (
        build_reply_text_from_store(
            rfq, quote, rfq_id, pipeline.company(), valid_days=settings.quote_valid_days
        )
        if quote
        else ""
    )
    artifacts = {a["kind"] for a in store.artifacts(rfq_id)}
    status = str(rfq.get("status") or "")
    # Снимки, сделанные до 2026 года, хранят 20%: такой расчёт нельзя одобрить
    # и отправить, пока он не пересчитан по действующей ставке.
    stale_vat = legacy_vat_rates(quote) if quote else []
    # Что можно делать с заявкой в этом статусе. Правила — здесь, а не в шаблоне:
    # кнопка, которая заведомо кончается ошибкой, — это не «функция», а ловушка.
    can_edit = status != RfqStatus.SENT.value
    can_approve = status in (RfqStatus.DRAFT_READY.value, RfqStatus.HUMAN_REVIEW.value) and not stale_vat
    can_reject = status not in (RfqStatus.SENT.value, RfqStatus.REJECTED.value)
    can_send = status in (RfqStatus.APPROVED.value, RfqStatus.SENT.value) and not stale_vat
    # Итоги считает пайплайн — те же строки, что уходят клиенту в письме и КП.
    totals = pipeline.quote_totals(quote) if quote else []
    # В поле «Кому» — чистый адрес: отправитель может быть записан
    # как «ООО «Ромашка» <sales@example.ru>» (заказ из кабинета).
    # Если адреса в отправителе нет, поле пустое: иначе браузер не даст отправить
    # форму из-за собственной проверки type="email".
    to_addr = sender_address(str(rfq.get("sender") or ""))
    if not EMAIL_RE.fullmatch(to_addr.lower()):
        to_addr = ""
    # Подсказки только для строк, где нужен человек: иначе страница с большим
    # прайсом снова распухнет. Для уверенных строк SKU уже известен.
    index = pipeline.price_index()
    candidates: dict[int, list] = {}
    for row in rfq["lines"]:
        if row["status"] != "ok" or not row["matched_sku"]:
            candidates[row["idx"]] = index.top(row["raw_text"], 5)
    return page(
        "rfq.html",
        request,
        rfq=rfq,
        quote=quote,
        totals=totals,
        stale_vat=stale_vat,
        to_addr=to_addr,
        can_edit=can_edit,
        can_approve=can_approve,
        can_reject=can_reject,
        can_send=can_send,
        reply_text=reply_text,
        artifacts=artifacts,
        candidates=candidates,
        llm_usage=store.llm_usage_for(rfq_id),
        timing=store.rfq_timing(rfq_id),
        sends=store.sent_history(rfq_id),
        stock=stock_for_lines(rfq["lines"], store.stock_map(), store.price_items()),
        assignee=store.user_by_id(rfq["assignee"]) if rfq.get("assignee") else None,
        users=store.users() if is_admin_request else [],
        decisions=store.decisions(rfq_id),
        flash=flash[:200],
    )


@app.post("/ui/rfq/{rfq_id}/approve")
async def ui_approve(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    user = current_user(request)
    if settings.approval_limit > 0 and user is not None and not user.is_admin:
        quote_data = store.get_quote(rfq_id) or {}
        total = Decimal(str(quote_data.get("total") or "0"))
        if total > settings.approval_limit:
            return back_to(
                rfq_id,
                f"сумма {total:.2f} ₽ выше лимита одобрения "
                f"({settings.approval_limit:.2f} ₽) — нужно одобрение администратора",
            )
    try:
        await run_in_threadpool(
            pipeline.approve,
            rfq_id,
            actor=actor_of(request),
            comment=str(form.get("comment", ""))[:500],
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    return RedirectResponse(
        f"/ui/rfq/{rfq_id}?flash={quote('одобрено, черновик ответа в outbox')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ui/rfq/{rfq_id}/reject")
async def ui_reject(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse("csrf failed", status_code=403)
    try:
        pipeline.reject(rfq_id, actor=actor_of(request), reason=str(form.get("reason", ""))[:500])
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    return RedirectResponse(
        f"/ui/rfq/{rfq_id}?flash={quote('отклонено')}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/ui/rfq/{rfq_id}/kp.html")
async def ui_kp_html(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = pipeline.out_dir() / f"{rfq_id}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="КП ещё не сформировано")
    return FileResponse(path, media_type="text/html", filename=f"kp-{rfq_id[:8]}.html")


@app.get("/ui/rfq/{rfq_id}/kp.pdf")
async def ui_kp_pdf(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = pipeline.out_dir() / f"{rfq_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF не сформирован — смотрите HTML или DOCX")
    return FileResponse(path, media_type="application/pdf", filename=f"kp-{rfq_id[:8]}.pdf")


@app.get("/ui/rfq/{rfq_id}/kp.docx")
@app.get("/ui/rfq/{rfq_id}/kp-office", include_in_schema=False)
async def ui_kp_docx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = pipeline.out_dir() / f"{rfq_id}.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="DOCX не сформирован")
    # без расширения в пути и с потоком байтов: CDN-ы придерживают офисные файлы
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"kp-{rfq_id[:8]}.docx",
    )


# ---------- правка строк ----------


def lines_from_db(rfq_id: str) -> tuple[dict, list[RfqLine]]:
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    return rfq, rfq_lines(rfq)


async def verified_form(request: Request):
    form = await request.form()
    if not verify_csrf(settings.session_secret, session_of(request), str(form.get(CSRF_FIELD, ""))):
        raise HTTPException(status_code=403, detail="csrf failed")
    return form


def back_to(rfq_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        f"/ui/rfq/{rfq_id}?flash={quote(message)}", status_code=status.HTTP_303_SEE_OTHER
    )


async def save_edits(rfq_id: str, lines: list[RfqLine], actor: str, note: str) -> RedirectResponse:
    try:
        await run_in_threadpool(pipeline.apply_edits, rfq_id, lines, actor=actor, note=note)
    except PermissionError as exc:
        return back_to(rfq_id, str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    return back_to(rfq_id, "строки сохранены, КП пересчитано")


def parse_qty(raw: str) -> Decimal | None:
    """Количество из формы: положительное конечное число или None.

    «Infinity», «NaN» и «1e999» — валидные с точки зрения Decimal строки, но не
    количество: раньше они доходили до округления и роняли запрос в 500.
    """
    value = raw.replace(",", ".").replace(" ", "").strip()
    if not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def qty_error(qty: Decimal | None) -> str:
    """Понятный текст ошибки для количества (пустой — количество принято)."""
    if qty is None:
        return "некорректное количество: нужно положительное число"
    if qty > settings.max_qty:
        return f"количество больше допустимого ({settings.max_qty:f}) — проверьте единицы"
    return ""


def resolve_price_item(value: str):
    """Позиция прайса по артикулу или по точному названию.

    Поле в карточке обещает «артикул или название», а сервер раньше принимал
    только артикул: набранное название отклонялось, хотя подсказка его находила.
    Название принимаем только при однозначном совпадении — угадывать позицию
    по неточному названию нельзя, цена считается по прайсу.
    """
    key = (value or "").strip()
    if not key:
        return None
    items = store.price_items()
    for item in items:
        if item.sku == key:
            return item
    lowered = key.casefold()
    matches = [item for item in items if item.name.strip().casefold() == lowered]
    return matches[0] if len(matches) == 1 else None


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/confirm")
async def ui_line_confirm(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)
    _rfq, lines = lines_from_db(rfq_id)
    target = next((line for line in lines if line.idx == idx), None)
    if target is None:
        return back_to(rfq_id, "строка не найдена")
    if not target.matched_sku:
        return back_to(rfq_id, "у строки нет подобранной позиции — выберите её вручную")
    # «Подтвердить» стоит в той же форме, что количество и артикул: человек мог
    # поправить их и нажать именно эту кнопку. Раньше правка молча терялась.
    raw_qty = str(form.get("qty", "")).strip()
    if raw_qty:
        qty = parse_qty(raw_qty)
        error = qty_error(qty)
        if error:
            return back_to(rfq_id, error)
        try:
            target.qty = quantity(qty)
        except ValidationError as exc:
            return back_to(rfq_id, str(exc))
    if target.qty is None or target.qty <= 0:
        # Без количества строка не попадает в расчёт, но выглядела бы как
        # подтверждённая («в прайсе») — человек считал бы её учтённой.
        return back_to(rfq_id, "сначала укажите количество — без него строка не войдёт в расчёт")
    target.status = LineStatus.OK
    target.note = ""
    return await save_edits(rfq_id, lines, actor_of(request), f"подтверждена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/update")
async def ui_line_update(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)

    raw_qty = str(form.get("qty", ""))
    qty = parse_qty(raw_qty)
    error = qty_error(qty)
    if error:
        return back_to(rfq_id, error)
    sku = str(form.get("sku", "")).strip()

    _rfq, lines = lines_from_db(rfq_id)
    target = next((line for line in lines if line.idx == idx), None)
    if target is None:
        return back_to(rfq_id, "строка не найдена")

    try:
        target.qty = quantity(qty)
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    if sku:
        item = resolve_price_item(sku)
        if item is None:
            return back_to(rfq_id, "такой позиции нет в прайс-листе — выберите её из подсказок")
        target.matched_sku = item.sku
        target.matched_name = item.name
        target.unit = item.unit
        target.status = LineStatus.OK
        target.note = ""
        target.match_score = 1.0
    return await save_edits(rfq_id, lines, actor_of(request), f"изменена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/delete")
async def ui_line_delete(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    await verified_form(request)
    _rfq, lines = lines_from_db(rfq_id)
    remaining = [line for line in lines if line.idx != idx]
    if len(remaining) == len(lines):
        return back_to(rfq_id, "строка не найдена")
    return await save_edits(rfq_id, remaining, actor_of(request), f"удалена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/add")
async def ui_line_add(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)

    sku = str(form.get("sku", "")).strip()
    raw_qty = str(form.get("qty", ""))
    qty = parse_qty(raw_qty)
    error = qty_error(qty)
    if not sku:
        return back_to(rfq_id, "выберите позицию из прайса")
    if error:
        return back_to(rfq_id, error)

    item = resolve_price_item(sku)
    if item is None:
        return back_to(rfq_id, "такой позиции нет в прайс-листе — выберите её из подсказок")

    _rfq, lines = lines_from_db(rfq_id)
    if len(lines) >= settings.max_rfq_lines:
        return back_to(
            rfq_id, f"в заявке уже {len(lines)} строк — больше {settings.max_rfq_lines} нельзя"
        )
    try:
        added_qty = quantity(qty)
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    lines.append(
        RfqLine(
            idx=len(lines) + 1,
            raw_text=f"{item.name} — {added_qty} {item.unit} (добавлено вручную)",
            qty=added_qty,
            unit=item.unit,
            matched_sku=item.sku,
            matched_name=item.name,
            match_score=1.0,
            status=LineStatus.OK,
            note="",
        )
    )
    return await save_edits(rfq_id, lines, actor_of(request), f"добавлена позиция {item.sku}")


@app.post("/ui/rfq/{rfq_id}/reprice")
async def ui_rfq_reprice(request: Request, rfq_id: str):
    """Пересчитывает расчёт по действующей ставке (снимки со старыми 20/18%)."""
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)
    # force=1 — исправление уже отправленного документа: заявка вернётся в
    # работу, чтобы выпустить корректное КП. Без него отправленную не трогаем.
    force = str(form.get("force", "")).strip() == "1"
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    if rfq["status"] == RfqStatus.SENT.value and not force:
        return back_to(
            rfq_id, "заявка уже отправлена — пересчёт изменил бы отправленный документ"
        )
    _rfq, lines = lines_from_db(rfq_id)
    if not lines:
        return back_to(rfq_id, "в заявке нет строк — пересчитывать нечего")
    # Позиции, которой уже нет в прайсе, в новом расчёте не будет — молча
    # выбрасывать строку из документа нельзя, пусть человек заменит или удалит.
    price_skus = {item.sku for item in store.price_items()}
    missing = sorted(
        {
            line.matched_sku
            for line in lines
            if line.status is LineStatus.OK and line.matched_sku and line.matched_sku not in price_skus
        }
    )
    if missing:
        return back_to(
            rfq_id,
            f"в прайсе нет позиции {', '.join(missing[:3])} — замените или удалите строку",
        )
    try:
        await run_in_threadpool(
            pipeline.apply_edits,
            rfq_id,
            lines,
            actor=actor_of(request),
            note="пересчёт по действующей ставке НДС",
            allow_sent=force,
        )
    except PermissionError as exc:
        return back_to(rfq_id, str(exc))
    except ValidationError as exc:
        return back_to(rfq_id, str(exc))
    store.log(
        rfq_id,
        "reprice_vat",
        actor_of(request),
        "снимок пересчитан по действующей ставке"
        + ("; отправленная возвращена в работу" if force else ""),
    )
    if force:
        return back_to(
            rfq_id, "расчёт исправлен и возвращён в работу — проверьте и отправьте заново"
        )
    return back_to(rfq_id, "расчёт пересчитан по текущему прайсу и действующей ставке")


@app.post("/ui/rfq/{rfq_id}/send")
async def ui_rfq_send(request: Request, rfq_id: str):
    """Отправляет готовое КП клиенту письмом с PDF и DOCX."""
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)
    if not limiter.allow(f"send:{rate_key(request)}"):
        return back_to(rfq_id, "слишком часто: подождите минуту")
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    if rfq["status"] == RfqStatus.REJECTED.value:
        return back_to(rfq_id, "заявка отклонена — отправка невозможна")
    if rfq["status"] not in (RfqStatus.APPROVED.value, RfqStatus.SENT.value):
        # README обещает «письмо уходит только после одобрения», а проверял это
        # только интерфейс: из карточки можно было отправить непроверенный расчёт.
        return back_to(rfq_id, "сначала одобрите КП — без проверки человеком не отправляем")

    to = sender_address(
        str(form.get("to", "")).strip() or str(rfq.get("sender", ""))
    ).lower()
    if not EMAIL_RE.fullmatch(to):
        return back_to(rfq_id, "нужен корректный адрес получателя")

    quote = store.get_quote(rfq_id)
    if quote is None:
        return back_to(rfq_id, "КП ещё не рассчитано")

    body = str(form.get("body", "")).strip() or build_reply_text_from_store(
        rfq, quote, rfq_id, pipeline.company(), valid_days=settings.quote_valid_days
    )
    attachments = load_attachments(pipeline.out_dir(), rfq_id, short=rfq_id[:8])
    if not attachments:
        # Письмо говорит «КП во вложении»: без файла клиент получит пустое
        # письмо. Лучше честно остановиться и попросить пересобрать документы.
        return back_to(rfq_id, "к письму нечего приложить: КП не собралось — обновите карточку")
    try:
        message = build_kp_message(
            sender=to,
            from_addr=mailer.from_addr or settings.smtp_user or "no-reply@localhost",
            subject=reply_subject(str(rfq.get("subject", ""))),
            body=body,
            attachments=attachments,
            reply_to=settings.smtp_user or "",
        )
    except ValueError:
        # Заголовки письма не принимают перевод строки: это попытка подставить
        # скрытого получателя, а не «необычный адрес».
        return back_to(rfq_id, "некорректный адрес получателя")
    try:
        await run_in_threadpool(mailer.send, message, to=to)
    except MailError as exc:
        store.enqueue_message(
            rfq_id=rfq_id,
            to_addr=to,
            subject=str(message["Subject"]),
            body=body,
            error=str(exc),
        )
        # Письмо не ушло — заявка остаётся одобренной и ждёт повтора, а не
        # «отправленной». Но если она уже была отправлена раньше (повторная
        # отправка не удалась), статус sent не откатываем: клиент письмо получил.
        current = (store.get_rfq(rfq_id) or {}).get("status")
        if current != RfqStatus.SENT.value:
            store.set_status(rfq_id, RfqStatus.APPROVED.value, force=True)
        store.log(rfq_id, "send_failed", actor_of(request), f"{to}: {exc}"[:300])
        if mailer.enabled:
            return back_to(rfq_id, f"не отправлено: {exc}. Письмо в очереди, повторю автоматически")
        return back_to(
            rfq_id,
            f"не отправлено: {exc}. Письмо в очереди; включите SMTP — и оно уйдёт при повторе",
        )

    store.set_status(rfq_id, RfqStatus.SENT.value, force=True)
    store.log(
        rfq_id,
        "sent",
        actor_of(request),
        f"кому {to}, вложений {len(attachments)}",
    )
    return back_to(rfq_id, f"КП отправлено на {to}")


@app.post("/ui/rfq/{rfq_id}/assign")
async def ui_rfq_assign(request: Request, rfq_id: str):
    """Менеджер берёт свободную заявку себе; администратор назначает любому."""
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)
    user = current_user(request)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")

    target = str(form.get("user_id", "")).strip() or (user.id if user else "")
    if not target:
        # Вход общим паролем: личности нет, и «взять себе» некому.
        return back_to(rfq_id, "войдите под своей учётной записью, чтобы взять заявку")
    if user is None or (not user.is_admin and rfq.get("assignee") not in (None, "", user.id)):
        return back_to(rfq_id, "эту заявку ведёт другой менеджер")
    if not user.is_admin and target != user.id:
        # Роль «менеджер берёт себе, админ назначает любому» раньше не держалась:
        # можно было передать заявку произвольной строке, и она «пропадала».
        return back_to(rfq_id, "менеджер может взять заявку только себе")
    if store.user_by_id(target) is None:
        return back_to(rfq_id, "такого сотрудника нет")

    store.set_assignee(rfq_id, target)
    store.log(rfq_id, "assigned", actor_of(request), f"исполнитель {target[:8]}")
    return back_to(rfq_id, "исполнитель назначен")


@app.get("/ui/rfq/{rfq_id}/export.xlsx")
async def ui_export_xlsx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    content = build_rfq_xlsx(rfq, store.get_quote(rfq_id), stock=store.stock_map())
    # Та же проверка, что в CLI: дубли артикулов и пустые поля до 1С доходить не должны.
    problems = check_1c_sheet(content)
    if problems:
        log.warning("выгрузка для 1С по заявке %s: %s", rfq_id, "; ".join(problems[:5]))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="zayavka-{rfq_id[:8]}.xlsx"'},
    )


# ---------- API ----------


class RfqIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    sender: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=200_000)
    message_id: str | None = Field(default=None, max_length=500)

    @field_validator("sender")
    @classmethod
    def sender_must_look_like_email(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("sender должен содержать @")
        return value


def api_auth(request: Request) -> None:
    if not _api_key_ok(request):
        raise HTTPException(status_code=401, detail="invalid api key")
    # X-Tenant с чужим (или общим) ключом не должен молча уехать в основную базу:
    # клиент просил одну базу, а записал бы в другую — это потеря данных.
    if (request.headers.get("x-tenant") or "").strip() and request_tenant_path(request) is None:
        raise HTTPException(status_code=401, detail="ключ не подходит к этому клиенту")


@app.get("/api/v1/health")
async def health() -> dict:
    return {"status": "ok", "price_items": store.price_count()}


@app.post("/api/v1/rfqs", dependencies=[Depends(api_auth)])
async def api_create_rfq(request: Request, payload: RfqIn) -> JSONResponse:
    # Лимит — на клиента, а не один на весь сервис: иначе шумный клиент
    # выбирает квоту у всех и получает 429 за чужие письма.
    tenant_key = (request.headers.get("x-tenant") or "").strip() or "default"
    if not limiter.allow(f"api:rfqs:{tenant_key}"):
        raise HTTPException(status_code=429, detail="rate limit")
    try:
        result = await run_in_threadpool(pipeline.process_text, 
            payload.text,
            sender=payload.sender,
            subject=payload.subject,
            message_id=payload.message_id,
            actor="api",
        )
    except NotAnRfq:
        raise HTTPException(
            status_code=422,
            detail="в письме нет позиций с количеством — это не заявка",
        ) from None
    return JSONResponse(
        {
            "rfq_id": result.rfq_id,
            "status": result.status,
            "lines": result.lines,
            "matched": result.matched,
            "duplicate": result.duplicate,
            "total": result.total,
            "kp_html": f"/ui/rfq/{result.rfq_id}/kp.html",
        },
        status_code=200 if result.duplicate else 201,
    )


@app.get("/api/v1/rfqs/{rfq_id}", dependencies=[Depends(api_auth)])
async def api_get_rfq(rfq_id: str) -> dict:
    ensure_rfq_id(rfq_id)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    return {"rfq": rfq, "quote": store.get_quote(rfq_id)}


@app.post("/api/v1/rfqs/{rfq_id}/approve", dependencies=[Depends(api_auth)])
async def api_approve(rfq_id: str) -> dict:
    ensure_rfq_id(rfq_id)
    try:
        result = await run_in_threadpool(pipeline.approve, rfq_id, actor="api")
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"rfq_id": result.rfq_id, "status": result.status}


@app.get("/api/v1/stats", dependencies=[Depends(api_auth)])
async def api_stats() -> dict:
    return store.stats()






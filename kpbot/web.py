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
import secrets
import string
import time
import uuid
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

from .config import settings
from .domain import LineStatus, RfqLine, RfqStatus, ValidationError, quantity
from .export import build_rfq_xlsx
from .mailer import MailError, Mailer, build_kp_message, load_attachments, reply_subject
from .pipeline import NotAnRfq, Pipeline, build_reply_text_from_store
from .pricelist import load_price_items
from .render import render_template
from .security import (
    CSRF_FIELD,
    SESSION_COOKIE,
    RateLimiter,
    constant_time_equal,
    csrf_for,
    make_session,
    make_user_session,
    user_from_session,
    verify_csrf,
    verify_session,
)
from .users import ROLE_ADMIN, User, normalize_email
from .store import Store
from .tenancy import TenantRegistry

from .logs import mask, register_settings_secrets, setup_logging

setup_logging()
register_settings_secrets(settings)
log = logging.getLogger("kpbot.web")

app = FastAPI(title="kpbot", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

settings.ensure_dirs()
registry = TenantRegistry(settings.tenants_db_path, settings.db_path)


class TenantStore:
    """Фасад над Store: база выбирается по клиенту из контекста запроса."""

    def __init__(self, default_path: Path) -> None:
        self.default_path = default_path

    def _target(self) -> Store:
        return Store(_tenant_db.get() or self.default_path)

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
        return PlainTextResponse("payload too large", status_code=413)
    tenant_token = _tenant_db.set(tenant_db_from_request(request))
    try:
        response = await call_next(request)
    finally:
        _tenant_db.reset(tenant_token)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if settings.public_base_url.lower().startswith("https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Request-ID", request_id)
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
    if not settings.is_production:
        return
    problems = settings.production_problems()
    if problems:
        raise RuntimeError(
            "нельзя запускаться в проде с такой конфигурацией:\n  - " + "\n  - ".join(problems)
        )
    log.info("конфигурация прода прошла проверку")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Для reverse proxy и systemd: без авторизации, ничего не раскрывает."""
    return {"status": "ok"}


# ---------- helpers ----------


RFQ_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def ensure_rfq_id(rfq_id: str) -> str:
    """Идентификатор заявки — только 32 hex-символа.

    Без проверки значение из адреса попадает в путь к файлу: это и обход
    каталогов, и 500-е на длинных строках.
    """
    if not RFQ_ID_RE.fullmatch(rfq_id or ""):
        raise HTTPException(status_code=404, detail="заявка не найдена")
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
_tenant_db: ContextVar[Path | None] = ContextVar("tenant_db", default=None)


def tenant_db_from_request(request: Request) -> Path | None:
    """База клиента по подписанной куке; None — основная база."""
    value = request.cookies.get(TENANT_COOKIE)
    if not value:
        return None
    tenant_id = user_from_session(settings.session_secret, value, 86400 * 30)
    if not tenant_id:
        return None
    tenant = registry.get(tenant_id)
    if not tenant or tenant["status"] != "active":
        return None
    path = Path(tenant["db_path"])
    return path if path.exists() else None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_of(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def current_user(request: Request) -> User | None:
    """Кто пришёл: из подписанной куки. None — не авторизован."""
    user_id = user_from_session(settings.session_secret, session_of(request), settings.session_ttl_seconds)
    if user_id:
        return store.user_by_id(user_id)
    return None


def is_authenticated(request: Request) -> bool:
    if current_user(request) is not None:
        return True
    # совместимость: одиночный пароль из конфигурации работает как вход администратора
    return verify_session(settings.session_secret, session_of(request), settings.session_ttl_seconds)


def is_admin_request(request: Request) -> bool:
    """Администратор: пользователь с ролью admin или вход общим паролем."""
    user = current_user(request)
    if user is not None:
        return user.is_admin
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
        **context,
    )
    return HTMLResponse(body, status_code=code)


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------- UI ----------


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/ui")


@app.get("/ui", response_class=HTMLResponse)
async def ui_inbox(request: Request, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    rfqs = store.list_rfqs(100, assignee=None if (user is None or user.is_admin) else user.id)
    return page(
        "inbox.html",
        request,
        active_page="inbox",
        rfqs=rfqs,
        users=store.users() if (user and user.is_admin) else [],
        report=store.report(30),
        flash=flash[:200],
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
        token = make_user_session(settings.session_secret, user.id, settings.session_ttl_seconds)
    elif not email and constant_time_equal(password, settings.ui_password):
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
                    error="файл больше 2 МБ")
    try:
        result = await run_in_threadpool(
            pipeline.process_eml_bytes, raw, actor=f"ui:{client_ip(request)}"
        )
    except NotAnRfq:
        return page("inbox.html", request, code=422, rfqs=store.list_rfqs(100),
                    error="в письме не найдено ни одной позиции с количеством — это не заявка")
    except ValueError as exc:
        return page("inbox.html", request, code=400, rfqs=store.list_rfqs(100), error=str(exc))

    target = f"/ui/rfq/{result.rfq_id}"
    flash = "письмо уже было обработано" if result.duplicate else "письмо разобрано"
    return RedirectResponse(f"{target}?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


async def apply_company_form(form) -> None:
    """Реквизиты, цвет и логотип — общая логика для настроек и визарда."""
    allowed = ("name", "inn", "kpp", "address", "phone", "email", "signer", "signature", "kp_intro")
    values = {f"company_{key}": str(form.get(key, "")).strip()[:500] for key in allowed}

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


@app.get("/ui/stats", response_class=HTMLResponse)
async def ui_stats(request: Request, days: str = "30"):
    """Сводка живёт на главной: отдельная страница больше не нужна."""
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    rfqs = store.list_rfqs(100, assignee=None if (user is None or user.is_admin) else user.id)
    return page(
        "inbox.html",
        request,
        active_page="inbox",
        rfqs=rfqs,
        users=store.users() if (user and user.is_admin) else [],
        report=store.report(safe_int(days, 30, 1, 365)),
    )


@app.get("/ui/prices", response_class=HTMLResponse)
async def ui_prices(request: Request, q: str = "", flash: str = ""):
    """Прайс: сколько позиций, когда обновляли, поиск и загрузка файла."""
    if not is_authenticated(request):
        return login_redirect(request)
    user = current_user(request)
    items = store.price_items()
    query = q.strip().lower()
    if query:
        items = [item for item in items if query in item.name.lower() or query in item.sku.lower()]
    return page(
        "prices.html",
        request,
        active_page="prices",
        items=items[:300],
        total=store.price_count(),
        shown=len(items),
        query=q.strip(),
        updated_at=store.price_updated_at(),
        can_edit=is_admin_request(request),
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
            f"/ui/prices?flash={quote('файл больше 2 МБ')}", status_code=status.HTTP_303_SEE_OTHER
        )

    suffix = ".xlsx" if filename.endswith(".xlsx") else ".csv"
    temp_path = settings.data_dir / f"price_upload{suffix}"
    temp_path.write_bytes(raw)
    try:
        items = load_price_items(temp_path)
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
    store.log_event("price_upload", f"ui:{client_ip(request)}", f"позиций {count}, файл {filename}")
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
    await apply_company_form(form)
    store.log_event("company_settings", f"ui:{client_ip(request)}", str(form.get("name", ""))[:120])
    return RedirectResponse(
        f"/ui/settings?flash={quote('настройки сохранены')}", status_code=status.HTTP_303_SEE_OTHER
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
    await apply_company_form(form)
    store.log_event("setup_company", f"ui:{client_ip(request)}", str(form.get("name", ""))[:120])
    return RedirectResponse(
        f"/ui/setup?flash={quote('шаг 1 готов: реквизиты сохранены')}", status_code=status.HTTP_303_SEE_OTHER
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
            f"/ui/setup?flash={quote('файл больше 2 МБ')}", status_code=status.HTTP_303_SEE_OTHER
        )
    suffix = ".xlsx" if filename.endswith(".xlsx") else ".csv"
    temp_path = settings.data_dir / f"price_upload{suffix}"
    temp_path.write_bytes(raw)
    try:
        items = load_price_items(temp_path)
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
    store.log_event("setup_price", f"ui:{client_ip(request)}", f"позиций {count}, файл {filename}")
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
    store.log_event("setup_mail", f"ui:{client_ip(request)}", message[:160])
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
    return page(
        "admin.html",
        request,
        active_page="admin",
        tab=tab if tab in ("state", "users", "learned", "events", "company") else "state",
        flash=flash[:200],
        **admin_context(request),
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


@app.get("/ui/ops", response_class=HTMLResponse)
async def ui_ops(request: Request, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    return page("ops.html", request, active_page="ops", tenants=registry.list(), flash=flash[:300])


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

    store.log_event("tenant_created", f"ui:{client_ip(request)}", f"{name} · {tenant['id']} · {email}")
    flash = f"клиент «{name}»: вход {email} · пароль {password} (покажется один раз)"
    return RedirectResponse(f"/ui/ops?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


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
    store.log_event("tenant_switch", f"ui:{client_ip(request)}", tenant["id"])
    return response


@app.post("/ui/ops/status")
async def ui_ops_status(request: Request):
    if not is_owner(request):
        raise HTTPException(status_code=403, detail="раздел владельца сервиса")
    form = await verified_form(request)
    tenant_id = str(form.get("tenant_id", "")).strip()
    status_value = str(form.get("status", "active")).strip()
    registry.set_status(tenant_id, status_value)
    store.log_event("tenant_status", f"ui:{client_ip(request)}", f"{tenant_id} → {status_value}")
    return RedirectResponse(f"/ui/ops?flash={quote('статус обновлён')}", status_code=status.HTTP_303_SEE_OTHER)


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
    store.log_event("user_created", f"ui:{client_ip(request)}", f"{user.email} · {user.role}")
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
    store.log_event("user_role", f"ui:{client_ip(request)}", f"{current.email} → {role}")
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
    store.log_event("user_password", f"ui:{client_ip(request)}", current.email)
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
    if current.role == ROLE_ADMIN and store.admin_count() <= 1:
        return RedirectResponse(
            f"/ui/admin/users?flash={quote('нельзя удалить последнего администратора')}", status_code=303
        )
    store.delete_user(user_id)
    store.log_event("user_deleted", f"ui:{client_ip(request)}", current.email)
    return RedirectResponse(f"/ui/admin/users?flash={quote('пользователь удалён')}", status_code=303)


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
    store.log_event("learned_forget", f"ui:{client_ip(request)}", key or "всё обучение")
    flash = f"удалено пар: {removed}"
    return RedirectResponse(f"/ui/admin/learned?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/admin/events", response_class=HTMLResponse)
async def ui_admin_events(request: Request):
    return await render_admin(request, tab="events")


@app.get("/ui/rfq/{rfq_id}", response_class=HTMLResponse)
async def ui_rfq(request: Request, rfq_id: str, flash: str = ""):
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
    reply_text = build_reply_text_from_store(rfq, quote, rfq_id, pipeline.company()) if quote else ""
    artifacts = {a["kind"] for a in store.artifacts(rfq_id)}
    return page(
        "rfq.html",
        request,
        rfq=rfq,
        quote=quote,
        reply_text=reply_text,
        artifacts=artifacts,
        items=store.price_items(),
        llm_usage=store.llm_usage_for(rfq_id),
        timing=store.rfq_timing(rfq_id),
        sends=store.sent_history(rfq_id),
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
    try:
        pipeline.approve(rfq_id, actor=f"ui:{client_ip(request)}", comment=str(form.get("comment", ""))[:500])
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
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
        pipeline.reject(rfq_id, actor=f"ui:{client_ip(request)}", reason=str(form.get("reason", ""))[:500])
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    return RedirectResponse(
        f"/ui/rfq/{rfq_id}?flash={quote('отклонено')}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/ui/rfq/{rfq_id}/kp.html")
async def ui_kp_html(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = settings.out_dir / f"{rfq_id}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="КП ещё не сформировано")
    return FileResponse(path, media_type="text/html", filename=f"kp-{rfq_id[:8]}.html")


@app.get("/ui/rfq/{rfq_id}/kp.pdf")
async def ui_kp_pdf(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = settings.out_dir / f"{rfq_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF не сформирован — смотрите HTML или DOCX")
    return FileResponse(path, media_type="application/pdf", filename=f"kp-{rfq_id[:8]}.pdf")


@app.get("/ui/rfq/{rfq_id}/kp.docx")
@app.get("/ui/rfq/{rfq_id}/kp-office", include_in_schema=False)
async def ui_kp_docx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    path = settings.out_dir / f"{rfq_id}.docx"
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
    lines: list[RfqLine] = []
    for row in rfq["lines"]:
        try:
            line_status = LineStatus(row["status"])
        except ValueError:
            line_status = LineStatus.INVALID
        lines.append(
            RfqLine(
                idx=int(row["idx"]),
                raw_text=row["raw_text"],
                qty=Decimal(row["qty"]) if row["qty"] else None,
                unit=row["unit"],
                matched_sku=row["matched_sku"],
                matched_name=row["matched_name"],
                match_score=float(row["score"] or 0),
                status=line_status,
                note=row["note"],
            )
        )
    return rfq, lines


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
    return back_to(rfq_id, "строки сохранены, КП пересчитано")


def parse_qty(raw: str) -> Decimal | None:
    value = raw.replace(",", ".").replace(" ", "").strip()
    if not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number > 0 else None


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/confirm")
async def ui_line_confirm(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    await verified_form(request)
    _rfq, lines = lines_from_db(rfq_id)
    target = next((line for line in lines if line.idx == idx), None)
    if target is None:
        return back_to(rfq_id, "строка не найдена")
    if not target.matched_sku:
        return back_to(rfq_id, "у строки нет подобранной позиции — выберите её вручную")
    target.status = LineStatus.OK
    target.note = ""
    return await save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"подтверждена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/update")
async def ui_line_update(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)

    qty = parse_qty(str(form.get("qty", "")))
    if qty is None:
        return back_to(rfq_id, "некорректное количество: нужно положительное число")
    sku = str(form.get("sku", "")).strip()

    _rfq, lines = lines_from_db(rfq_id)
    target = next((line for line in lines if line.idx == idx), None)
    if target is None:
        return back_to(rfq_id, "строка не найдена")

    target.qty = quantity(qty)
    if sku:
        item = next((candidate for candidate in store.price_items() if candidate.sku == sku), None)
        if item is None:
            return back_to(rfq_id, "такой позиции нет в прайс-листе")
        target.matched_sku = item.sku
        target.matched_name = item.name
        target.unit = item.unit
        target.status = LineStatus.OK
        target.note = ""
        target.match_score = 1.0
    return await save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"изменена строка {idx}")


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
    return await save_edits(rfq_id, remaining, f"ui:{client_ip(request)}", f"удалена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/add")
async def ui_line_add(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    form = await verified_form(request)

    sku = str(form.get("sku", "")).strip()
    qty = parse_qty(str(form.get("qty", "")))
    if not sku:
        return back_to(rfq_id, "выберите позицию из прайса")
    if qty is None:
        return back_to(rfq_id, "некорректное количество: нужно положительное число")

    item = next((candidate for candidate in store.price_items() if candidate.sku == sku), None)
    if item is None:
        return back_to(rfq_id, "такой позиции нет в прайс-листе")

    _rfq, lines = lines_from_db(rfq_id)
    lines.append(
        RfqLine(
            idx=len(lines) + 1,
            raw_text=f"{item.name} — {qty} {item.unit} (добавлено вручную)",
            qty=quantity(qty),
            unit=item.unit,
            matched_sku=item.sku,
            matched_name=item.name,
            match_score=1.0,
            status=LineStatus.OK,
            note="",
        )
    )
    return await save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"добавлена позиция {item.sku}")


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

    to = normalize_email(str(form.get("to", "")).strip() or str(rfq.get("sender", "")))
    if "@" not in to:
        return back_to(rfq_id, "нужен корректный адрес получателя")

    quote = store.get_quote(rfq_id)
    if quote is None:
        return back_to(rfq_id, "КП ещё не рассчитано")

    body = str(form.get("body", "")).strip() or build_reply_text_from_store(rfq, quote, rfq_id, pipeline.company())
    attachments = load_attachments(settings.out_dir, rfq_id, short=rfq_id[:8])
    message = build_kp_message(
        sender=to,
        from_addr=mailer.from_addr or settings.smtp_user or "no-reply@localhost",
        subject=reply_subject(str(rfq.get("subject", ""))),
        body=body,
        attachments=attachments,
        reply_to=settings.smtp_user or "",
    )
    try:
        mailer.send(message, to=to)
    except MailError as exc:
        store.enqueue_message(
            rfq_id=rfq_id,
            to_addr=to,
            subject=str(message["Subject"]),
            body=body,
            error=str(exc),
        )
        store.log(rfq_id, "send_failed", f"ui:{client_ip(request)}", f"{to}: {exc}"[:300])
        return back_to(rfq_id, f"не отправлено: {exc}. Письмо в очереди, повторю автоматически")

    store.log(
        rfq_id,
        "sent",
        f"ui:{client_ip(request)}",
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
        return back_to(rfq_id, "не выбран исполнитель")
    if user is None or (not user.is_admin and rfq.get("assignee") not in (None, "", user.id)):
        return back_to(rfq_id, "эту заявку ведёт другой менеджер")

    store.set_assignee(rfq_id, target)
    store.log(rfq_id, "assigned", f"ui:{client_ip(request)}", f"исполнитель {target[:8]}")
    return back_to(rfq_id, "исполнитель назначен")


@app.get("/ui/rfq/{rfq_id}/export.xlsx")
async def ui_export_xlsx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    ensure_rfq_id(rfq_id)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    content = build_rfq_xlsx(rfq, store.get_quote(rfq_id))
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


def api_auth(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or not constant_time_equal(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/api/v1/health")
async def health() -> dict:
    return {"status": "ok", "price_items": store.price_count()}


@app.post("/api/v1/rfqs", dependencies=[Depends(api_auth)])
async def api_create_rfq(payload: RfqIn) -> JSONResponse:
    if not limiter.allow("api:rfqs"):
        raise HTTPException(status_code=429, detail="rate limit")
    result = await run_in_threadpool(pipeline.process_text, 
        payload.text,
        sender=payload.sender,
        subject=payload.subject,
        message_id=payload.message_id,
        actor="api",
    )
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
        result = pipeline.approve(rfq_id, actor="api")
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    return {"rfq_id": result.rfq_id, "status": result.status}


@app.get("/api/v1/stats", dependencies=[Depends(api_auth)])
async def api_stats() -> dict:
    return store.stats()






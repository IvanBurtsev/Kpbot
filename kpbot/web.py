"""HTTP-слой: интерфейс рекрутера-продавца и версионированное API.

Принципы:
  * интерфейс закрыт паролем (в письмах ПДн), API — ключом в заголовке;
  * любая форма проверяет CSRF; cookie HttpOnly + SameSite=Lax;
  * размер тела ограничен до чтения файла;
  * весь пользовательский текст выводится через autoescape.
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .domain import LineStatus, RfqLine, RfqStatus, quantity
from .export import build_rfq_xlsx
from .pipeline import NotAnRfq, Pipeline, build_reply_text_from_store
from .render import render_template
from .security import (
    CSRF_FIELD,
    SESSION_COOKIE,
    RateLimiter,
    constant_time_equal,
    csrf_for,
    make_session,
    verify_csrf,
    verify_session,
)
from .store import Store

from .logs import mask, register_settings_secrets, setup_logging

setup_logging()
register_settings_secrets(settings)
log = logging.getLogger("kpbot.web")

app = FastAPI(title="kpbot", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

settings.ensure_dirs()
store = Store(settings.db_path)
pipeline = Pipeline(store, settings)
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
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)
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


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_of(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def is_authenticated(request: Request) -> bool:
    return verify_session(settings.session_secret, session_of(request), settings.session_ttl_seconds)


def page(name: str, request: Request, *, code: int = 200, **context: object) -> HTMLResponse:
    session = session_of(request)
    body = render_template(
        name,
        authenticated=is_authenticated(request),
        csrf_token=csrf_for(settings.session_secret, session),
        price_count=store.price_count(),
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
    return page(
        "inbox.html",
        request,
        rfqs=store.list_rfqs(100),
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
    password = str(form.get("password", ""))
    if not constant_time_equal(password, settings.ui_password):
        log.warning("неудачный вход с %s", client_ip(request))
        return page("login.html", request, code=401, error="неверный пароль")
    response = RedirectResponse("/ui", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(settings.session_secret, settings.session_ttl_seconds),
        httponly=True,
        samesite="lax",
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
    if not limiter.allow(f"upload:{client_ip(request)}"):
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
        result = pipeline.process_eml_bytes(raw, actor=f"ui:{client_ip(request)}")
    except NotAnRfq:
        return page("inbox.html", request, code=422, rfqs=store.list_rfqs(100),
                    error="в письме не найдено ни одной позиции с количеством — это не заявка")
    except ValueError as exc:
        return page("inbox.html", request, code=400, rfqs=store.list_rfqs(100), error=str(exc))

    target = f"/ui/rfq/{result.rfq_id}"
    flash = "письмо уже было обработано" if result.duplicate else "письмо разобрано"
    return RedirectResponse(f"{target}?flash={quote(flash)}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/ui/rfq/{rfq_id}", response_class=HTMLResponse)
async def ui_rfq(request: Request, rfq_id: str, flash: str = ""):
    if not is_authenticated(request):
        return login_redirect(request)
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        return page("inbox.html", request, code=404, rfqs=store.list_rfqs(100), error="заявка не найдена")
    quote = store.get_quote(rfq_id)
    reply_text = build_reply_text_from_store(rfq, quote, rfq_id) if quote else ""
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
        decisions=store.decisions(rfq_id),
        flash=flash[:200],
    )


@app.post("/ui/rfq/{rfq_id}/approve")
async def ui_approve(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
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
    path = settings.out_dir / f"{rfq_id}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="КП ещё не сформировано")
    return FileResponse(path, media_type="text/html", filename=f"kp-{rfq_id[:8]}.html")


@app.get("/ui/rfq/{rfq_id}/kp.pdf")
async def ui_kp_pdf(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
    path = settings.out_dir / f"{rfq_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF не сформирован — смотрите HTML или DOCX")
    return FileResponse(path, media_type="application/pdf", filename=f"kp-{rfq_id[:8]}.pdf")


@app.get("/ui/rfq/{rfq_id}/kp.docx")
@app.get("/ui/rfq/{rfq_id}/kp-office", include_in_schema=False)
async def ui_kp_docx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
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


def save_edits(rfq_id: str, lines: list[RfqLine], actor: str, note: str) -> RedirectResponse:
    try:
        pipeline.apply_edits(rfq_id, lines, actor=actor, note=note)
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
    await verified_form(request)
    _rfq, lines = lines_from_db(rfq_id)
    target = next((line for line in lines if line.idx == idx), None)
    if target is None:
        return back_to(rfq_id, "строка не найдена")
    if not target.matched_sku:
        return back_to(rfq_id, "у строки нет подобранной позиции — выберите её вручную")
    target.status = LineStatus.OK
    target.note = ""
    return save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"подтверждена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/update")
async def ui_line_update(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
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
    return save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"изменена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/{idx}/delete")
async def ui_line_delete(request: Request, rfq_id: str, idx: int):
    if not is_authenticated(request):
        return login_redirect(request)
    await verified_form(request)
    _rfq, lines = lines_from_db(rfq_id)
    remaining = [line for line in lines if line.idx != idx]
    if len(remaining) == len(lines):
        return back_to(rfq_id, "строка не найдена")
    return save_edits(rfq_id, remaining, f"ui:{client_ip(request)}", f"удалена строка {idx}")


@app.post("/ui/rfq/{rfq_id}/lines/add")
async def ui_line_add(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
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
    return save_edits(rfq_id, lines, f"ui:{client_ip(request)}", f"добавлена позиция {item.sku}")


@app.get("/ui/rfq/{rfq_id}/export.xlsx")
async def ui_export_xlsx(request: Request, rfq_id: str):
    if not is_authenticated(request):
        return login_redirect(request)
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
    result = pipeline.process_text(
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
    rfq = store.get_rfq(rfq_id)
    if rfq is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    return {"rfq": rfq, "quote": store.get_quote(rfq_id)}


@app.post("/api/v1/rfqs/{rfq_id}/approve", dependencies=[Depends(api_auth)])
async def api_approve(rfq_id: str) -> dict:
    try:
        result = pipeline.approve(rfq_id, actor="api")
    except KeyError:
        raise HTTPException(status_code=404, detail="заявка не найдена") from None
    return {"rfq_id": result.rfq_id, "status": result.status}


@app.get("/api/v1/stats", dependencies=[Depends(api_auth)])
async def api_stats() -> dict:
    return store.stats()
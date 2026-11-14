from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from auth import (
    MIN_PASSWORD_LENGTH,
    ROLE_LABELS,
    ROLE_TITLES,
    STAFF,
    authenticate,
    change_own_password,
    reset_admin_password,
)
from catalog import (
    CATALOG_DELETE_CONFIRM,
    CatalogError,
    CatalogNotFound,
    filter_products,
    present_product,
)
from roster import (
    PURPOSE_AUTO,
    PURPOSE_CATALOG,
    PURPOSE_CUSTOMERS,
    PURPOSE_ORDERS,
    ROSTER_CONFIRM,
    ROSTER_DELETE_CONFIRM,
    ROSTER_EDIT_CONFIRM,
    RosterError,
    RosterNotFound,
    detect_purpose,
    filter_customers,
    present_customer,
)
from scan import mark_scan, post_scan, scan_is_limited
from storage import Storage, parse_iso_date, record_date_text

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif",
    ".pdf", ".txt", ".zip", ".xlsx", ".xls", ".csv", ".doc", ".docx",
}
MAX_FILES = 40
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024
DELETE_CONFIRM = "確定刪除此提交？"
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_TRIES = 8
PURPOSE_CHOICES = {PURPOSE_AUTO, PURPOSE_ORDERS, PURPOSE_CUSTOMERS, PURPOSE_CATALOG}
SCAN_SENT = "已發出掃描要求，做好會把 Excel 放回這裡。"
SCAN_OFFLINE = "即時掃描還沒接上。可以在 Grok Bot 對話裡請它掃描。"
SCAN_FAILED = "暫時無法發出掃描要求，請稍後再試，或在 Grok Bot 對話裡請它掃描。"
SCAN_LIMITED = "掃描要求太頻密，請稍候再按。"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    same_site="lax",
    https_only=os.environ.get("HTTPS_ONLY", "true").lower() != "false",
    session_cookie="wa_order_session",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
storage = Storage()
_login_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_attempts[ip] = hits
    return len(hits) >= LOGIN_MAX_TRIES


def _record_fail(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _clear_fails(ip: str) -> None:
    _login_attempts.pop(ip, None)


def _safe_filename(name: str) -> str:
    name = Path(name or "").name
    name = re.sub(r"[^\w.\u4e00-\u9fff\-()+ ]+", "_", name, flags=re.UNICODE)
    name = name.strip().strip(".")
    return name or "file"


def _current_user(request: Request) -> str | None:
    user = request.session.get("user")
    if not isinstance(user, str) or user not in STAFF:
        return None
    generation = storage.password_generation(user)
    session_gen = request.session.get("pw_gen")
    if not isinstance(generation, int) or not isinstance(session_gen, int):
        return None
    if generation != session_gen:
        return None
    return user


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _valid_csrf(request: Request, token: str | None) -> bool:
    expected = request.session.get("csrf")
    if not isinstance(expected, str) or not isinstance(token, str) or not expected or not token:
        return False
    if len(expected) != len(token):
        return False
    return hmac.compare_digest(expected, token)


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    user = _current_user(request)
    role = STAFF.get(user) if user else None
    return {
        "request": request,
        "user": user,
        "role": role,
        "role_label": ROLE_LABELS.get(role or "", ""),
        "role_title": ROLE_TITLES.get(role or "", ""),
        "csrf": _csrf_token(request),
        **extra,
    }


def _form_purpose(value: str | None) -> str:
    text = (value or "").strip()
    if text in PURPOSE_CHOICES:
        return text
    return PURPOSE_AUTO


def parse_record_dates(
    mode: str | None,
    day: str | None,
    start: str | None,
    end: str | None,
) -> tuple[str | None, str | None]:
    """Optional Hong Kong calendar window. Blank means process every date in the files.

    A single day is returned as the same start and end. Raises ValueError with a
    Traditional Chinese message when the chosen dates are not usable.
    """
    selected = (mode or "").strip()
    # Explicit 「不指定」 wins, including dates still posted by hidden inputs.
    if selected == "":
        return None, None
    if selected == "day":
        try:
            chosen = parse_iso_date(day)
        except ValueError as exc:
            raise ValueError("日期格式不正確，請選擇有效的日期。") from exc
        if chosen is None:
            return None, None
        iso = chosen.isoformat()
        return iso, iso
    if selected == "range":
        try:
            start_day = parse_iso_date(start)
            end_day = parse_iso_date(end)
        except ValueError as exc:
            raise ValueError("日期格式不正確，請選擇有效的日期。") from exc
        if start_day is None and end_day is None:
            return None, None
        if start_day is not None and end_day is not None and end_day < start_day:
            raise ValueError("結束日期不可早於開始日期。")
        if start_day is None and end_day is not None:
            iso = end_day.isoformat()
            return iso, iso
        if end_day is None and start_day is not None:
            iso = start_day.isoformat()
            return iso, iso
        assert start_day is not None and end_day is not None
        return start_day.isoformat(), end_day.isoformat()
    raise ValueError("請選擇單日或日期範圍。")


def _echo_date(value: str | None) -> str:
    return record_date_text(value)


def _login_view(request: Request, error: str | None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(request, error=error),
        status_code=status_code,
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return _login_view(request, error=None)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    ip = _client_ip(request)
    username = (username or "").strip()
    if not _valid_csrf(request, csrf):
        return _login_view(request, error="請重新整理後再試。", status_code=400)
    if _rate_limited(ip):
        return _login_view(request, error="嘗試次數過多，請 15 分鐘後再試。", status_code=429)
    storage.refresh_password_records()
    record = storage.get_password_record(username)
    ok, promoted = authenticate(username, password, record)
    if not ok:
        _record_fail(ip)
        return _login_view(request, error="帳號或密碼不正確。", status_code=401)
    if promoted is not None:
        try:
            storage.save_password_record(username, promoted)
        except Exception:
            return _login_view(request, error="暫時無法保存登入資料，請稍後再試。", status_code=500)
        generation = int(promoted["generation"])
    else:
        generation = int(record["generation"]) if record else 0
    _clear_fails(ip)
    request.session.clear()
    request.session["user"] = username
    request.session["pw_gen"] = generation
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf: str = Form("")) -> Response:
    if _valid_csrf(request, csrf):
        request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _home_response(
    request: Request,
    *,
    error: str | None,
    notice: str | None,
    status_code: int = 200,
    form_mode: str = "",
    form_day: str = "",
    form_start: str = "",
    form_end: str = "",
    form_purpose: str = PURPOSE_AUTO,
    scan_note: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(
            request,
            items=storage.list_submissions(),
            error=error,
            notice=notice,
            scan_note=scan_note,
            form_mode=form_mode if form_mode in {"", "day", "range"} else "",
            form_day=_echo_date(form_day),
            form_start=_echo_date(form_start),
            form_end=_echo_date(form_end),
            form_purpose=_form_purpose(form_purpose),
        ),
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return _home_response(
        request,
        error=None,
        notice=request.query_params.get("notice"),
        form_purpose=_form_purpose(request.query_params.get("purpose")),
    )


@app.post("/submit")
async def submit(
    request: Request,
    kind: str = Form(""),
    note: str = Form(""),
    csrf: str = Form(""),
    record_mode: str = Form(""),
    record_day: str = Form(""),
    record_date_start: str = Form(""),
    record_date_end: str = Form(""),
    purpose: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form_mode = (record_mode or "").strip()
    posted_purpose = (purpose or "").strip()
    echoed_purpose = posted_purpose if posted_purpose in PURPOSE_CHOICES else PURPOSE_AUTO
    if not _valid_csrf(request, csrf):
        return _home_response(
            request,
            error="請重新整理後再提交。",
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    note = (note or "").strip()[:2000]
    if posted_purpose not in {"", *PURPOSE_CHOICES}:
        return _home_response(
            request,
            error="請選擇上載用途。",
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=PURPOSE_AUTO,
        )
    try:
        record_start, record_end = parse_record_dates(
            form_mode, record_day, record_date_start, record_date_end
        )
    except ValueError as exc:
        return _home_response(
            request,
            error=str(exc),
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    saved: list[tuple[str, bytes]] = []
    total = 0
    errors: list[str] = []
    for upload in files:
        raw_name = upload.filename or ""
        if not raw_name:
            continue
        name = _safe_filename(raw_name)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            errors.append(f"「{name}」檔案類型不允許。")
            continue
        data = await upload.read()
        if not data:
            continue
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"「{name}」超過單檔 40MB 上限。")
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            errors.append("今次上載總容量超過 80MB 上限。")
            break
        saved.append((name, data))
    if errors or not saved:
        msg = "；".join(errors) if errors else "請至少選擇一個檔案。"
        return _home_response(
            request,
            error=msg,
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    try:
        chosen_purpose = detect_purpose(echoed_purpose, [name for name, _payload in saved])
    except ValueError as exc:
        return _home_response(
            request,
            error=str(exc),
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    try:
        sub_id = storage.create_submission(
            user=user,
            kind=kind,
            note=note,
            files=saved,
            record_date_start=record_start,
            record_date_end=record_end,
            purpose=chosen_purpose,
        )
    except Exception:
        return _home_response(
            request,
            error="儲存失敗，請稍後再試或聯絡管理員。",
            notice=None,
            status_code=500,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    return RedirectResponse(f"/submission/{quote(sub_id)}?notice=submitted", status_code=303)


@app.get("/submission/{sub_id}", response_class=HTMLResponse)
def submission_page(request: Request, sub_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sub_id):
        return RedirectResponse("/", status_code=303)
    item = storage.get_submission(sub_id)
    if not item:
        return templates.TemplateResponse(
            request, "missing.html", _ctx(request), status_code=404
        )
    return templates.TemplateResponse(
        request,
        "submission.html",
        _ctx(request, item=item, notice=request.query_params.get("notice"), scan_note=None),
    )


def _delete_page(
    request: Request,
    item: dict[str, Any],
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "delete.html",
        _ctx(request, item=item, error=error, delete_confirm=DELETE_CONFIRM),
        status_code=status_code,
    )


def _delete_forbidden(request: Request, item: dict[str, Any] | None) -> Response:
    if not item:
        return templates.TemplateResponse(
            request, "missing.html", _ctx(request), status_code=404
        )
    return templates.TemplateResponse(
        request,
        "submission.html",
        _ctx(request, item=item, notice=None, error="沒有權限。", scan_note=None),
        status_code=403,
    )


@app.get("/submission/{sub_id}/delete", response_class=HTMLResponse)
def delete_confirm(request: Request, sub_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sub_id):
        return RedirectResponse("/", status_code=303)
    item = storage.get_submission(sub_id)
    if STAFF.get(user) != "super_admin":
        return _delete_forbidden(request, item)
    if not item:
        return templates.TemplateResponse(
            request, "missing.html", _ctx(request), status_code=404
        )
    return _delete_page(request, item)


@app.post("/submission/{sub_id}/delete")
async def delete_submit(
    request: Request,
    sub_id: str,
    csrf: str = Form(""),
    confirm: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sub_id):
        return RedirectResponse("/", status_code=303)
    item = storage.get_submission(sub_id)
    if STAFF.get(user) != "super_admin":
        return _delete_forbidden(request, item)
    if not item:
        return templates.TemplateResponse(
            request, "missing.html", _ctx(request), status_code=404
        )
    if not _valid_csrf(request, csrf):
        return _delete_page(request, item, error="請重新整理後再試。", status_code=400)
    if confirm != DELETE_CONFIRM:
        return _delete_page(request, item, error="請先確認再刪除。", status_code=400)
    if item.get("deleted"):
        return RedirectResponse("/?notice=deleted", status_code=303)
    try:
        updated = storage.soft_delete_submission(sub_id, user)
    except PermissionError:
        return _delete_forbidden(request, item)
    except Exception:
        return _delete_page(request, item, error="暫時無法刪除，請稍後再試。", status_code=500)
    if not updated:
        return templates.TemplateResponse(
            request, "missing.html", _ctx(request), status_code=404
        )
    return RedirectResponse("/?notice=deleted", status_code=303)


@app.get("/download/{sub_id}/{kind}/{filename}")
def download(request: Request, sub_id: str, kind: str, filename: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if kind not in {"files", "results"}:
        return Response(status_code=404)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sub_id):
        return Response(status_code=404)
    safe = _safe_filename(filename)
    path = storage.local_file_path(sub_id, kind, safe)
    if not path or not path.is_file():
        return Response("找不到檔案。", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(path, filename=safe)


ACCOUNT_NOTICES = {
    "password-updated": "密碼已更新。",
    "reset-updated": "已為同事設定新密碼。請私下告知對方。",
}


def _staff_rows() -> list[dict[str, Any]]:
    storage.refresh_password_records()
    rows: list[dict[str, Any]] = []
    for name, role in STAFF.items():
        if role != "admin":
            continue
        record = storage.get_password_record(name)
        rows.append(
            {
                "name": name,
                "role_label": ROLE_LABELS[role],
                "role_title": ROLE_TITLES[role],
                "ready": bool(record and record.get("hash")),
            }
        )
    return rows


def _account_page(
    request: Request,
    error: str | None = None,
    reset_error: str | None = None,
    status_code: int = 200,
) -> Response:
    notice_code = request.query_params.get("notice") or ""
    staff = _staff_rows() if STAFF.get(_current_user(request) or "") == "super_admin" else []
    return templates.TemplateResponse(
        request,
        "account.html",
        _ctx(
            request,
            error=error,
            reset_error=reset_error,
            notice=ACCOUNT_NOTICES.get(notice_code),
            staff=staff,
            min_password=MIN_PASSWORD_LENGTH,
        ),
        status_code=status_code,
    )


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    return _account_page(request)


@app.post("/account/password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not _valid_csrf(request, csrf):
        return _account_page(request, error="請重新整理後再試。", status_code=400)
    ip = _client_ip(request)
    if _rate_limited(ip):
        return _account_page(request, error="嘗試次數過多，請 15 分鐘後再試。", status_code=429)
    storage.refresh_password_records()
    record = storage.get_password_record(user)
    error, updated = change_own_password(user, current_password, new_password, confirm_password, record)
    if error or updated is None:
        if error == "目前密碼不正確。":
            _record_fail(ip)
        return _account_page(request, error=error or "暫時無法更新密碼。", status_code=400)
    try:
        storage.save_password_record(user, updated)
    except Exception:
        return _account_page(request, error="暫時無法保存密碼，請稍後再試。", status_code=500)
    _clear_fails(ip)
    request.session["pw_gen"] = int(updated["generation"])
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/account?notice=password-updated", status_code=303)


@app.post("/account/reset")
async def reset_password_submit(
    request: Request,
    target: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if STAFF.get(user) != "super_admin":
        return _account_page(request, error="沒有權限。", status_code=403)
    if not _valid_csrf(request, csrf):
        return _account_page(request, reset_error="請重新整理後再試。", status_code=400)
    target = (target or "").strip()
    storage.refresh_password_records()
    existing = storage.get_password_record(target)
    error, updated = reset_admin_password(user, target, new_password, confirm_password, existing)
    if error or updated is None:
        return _account_page(request, reset_error=error or "暫時無法設定密碼。", status_code=400)
    try:
        storage.save_password_record(target, updated)
    except Exception:
        return _account_page(request, reset_error="暫時無法保存密碼，請稍後再試。", status_code=500)
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/account?notice=reset-updated", status_code=303)


def _roster_notice(code: str | None) -> str | None:
    return {
        "deleted": "已刪除此顧客。",
        "confirmed": "已確認這位顧客。",
        "saved": "已儲存更正。",
    }.get(code or "")


def _roster_rows(query: str, which: str) -> tuple[list[dict[str, Any]], int, str]:
    document = storage.get_roster_document()
    customers = list(document.get("customers") or [])
    selected = which if which in {"all", "missing_phone", "pending"} else "all"
    shown = filter_customers(customers, query, selected)
    return [present_customer(row) for row in shown], len(customers), selected


def _customer_row(customer_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", customer_id):
        return None
    for row in storage.get_roster_document().get("customers") or []:
        if row.get("id") == customer_id:
            return present_customer(row)
    return None


def _related_submissions(row: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for sub_id in row.get("submission_ids") or []:
        item = storage.get_submission(str(sub_id))
        if item:
            found.append(item)
    return found


def _customers_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    query: str = "",
    which: str = "all",
) -> Response:
    rows, total, selected = _roster_rows(query, which)
    return templates.TemplateResponse(
        request,
        "customers.html",
        _ctx(
            request,
            error=error,
            notice=_roster_notice(request.query_params.get("notice")),
            rows=rows,
            total=total,
            query=query,
            roster_filter=selected,
        ),
        status_code=status_code,
    )


def _customer_page(
    request: Request,
    customer: dict[str, Any] | None,
    *,
    error: str | None = None,
    status_code: int = 200,
    template: str = "customer.html",
    extra: dict[str, Any] | None = None,
) -> Response:
    payload = {
        "customer": customer,
        "error": error,
        "notice": _roster_notice(request.query_params.get("notice")),
        "related": _related_submissions(customer) if customer else [],
        "roster_confirm": ROSTER_CONFIRM,
        "roster_edit_confirm": ROSTER_EDIT_CONFIRM,
        "roster_delete_confirm": ROSTER_DELETE_CONFIRM,
    }
    if extra:
        payload.update(extra)
    return templates.TemplateResponse(
        request,
        template,
        _ctx(request, **payload),
        status_code=status_code,
    )


def _roster_forbidden(request: Request, customer: dict[str, Any] | None) -> Response:
    if customer:
        return _customer_page(request, customer, error="沒有權限。", status_code=403)
    return _customer_page(request, None, error="沒有權限。", status_code=403)


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    return _customers_page(
        request,
        query=(request.query_params.get("q") or "").strip(),
        which=(request.query_params.get("filter") or "all").strip(),
    )


@app.get("/customers/roster.xlsx")
def download_roster(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    workbook = storage.roster_workbook()
    filename = quote("顧客名冊.xlsx")
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"roster.xlsx\"; filename*=UTF-8''{filename}"},
    )


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_page(request: Request, customer_id: str) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if not customer:
        return _customer_page(request, None, status_code=404)
    return _customer_page(request, customer)


@app.get("/customers/{customer_id}/confirm", response_class=HTMLResponse)
def customer_confirm_page(request: Request, customer_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    return _customer_page(request, customer, template="customer_confirm.html")


@app.post("/customers/{customer_id}/confirm")
async def customer_confirm_submit(
    request: Request,
    customer_id: str,
    csrf: str = Form(""),
    confirm: str = Form(""),
    phone_choice: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    if not _valid_csrf(request, csrf):
        return _customer_page(
            request,
            customer,
            error="請重新整理後再試。",
            status_code=400,
            template="customer_confirm.html",
        )
    if confirm != ROSTER_CONFIRM:
        return _customer_page(
            request,
            customer,
            error="請先確認再標記。",
            status_code=400,
            template="customer_confirm.html",
        )
    try:
        storage.confirm_customer(user, customer_id, phone_choice)
    except RosterNotFound:
        return _customer_page(request, None, status_code=404)
    except PermissionError:
        return _roster_forbidden(request, customer)
    except RosterError as exc:
        return _customer_page(
            request,
            _customer_row(customer_id) or customer,
            error=str(exc),
            status_code=400,
            template="customer_confirm.html",
        )
    except Exception:
        return _customer_page(
            request,
            customer,
            error="暫時無法儲存，請稍後再試。",
            status_code=500,
            template="customer_confirm.html",
        )
    return RedirectResponse(f"/customers/{quote(customer_id)}?notice=confirmed", status_code=303)


@app.get("/customers/{customer_id}/edit", response_class=HTMLResponse)
def customer_edit_page(request: Request, customer_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    return _customer_page(
        request,
        customer,
        template="customer_edit.html",
        extra={"edit_name": customer["name"], "edit_phone": customer["phone"], "edit_note": customer["note"]},
    )


@app.post("/customers/{customer_id}/edit")
async def customer_edit_submit(
    request: Request,
    customer_id: str,
    csrf: str = Form(""),
    confirm: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    note: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    edit_extra = {"edit_name": name, "edit_phone": phone, "edit_note": note}
    if not _valid_csrf(request, csrf):
        return _customer_page(
            request,
            customer,
            error="請重新整理後再試。",
            status_code=400,
            template="customer_edit.html",
            extra=edit_extra,
        )
    if confirm != ROSTER_EDIT_CONFIRM:
        return _customer_page(
            request,
            customer,
            error="請先確認再儲存。",
            status_code=400,
            template="customer_edit.html",
            extra=edit_extra,
        )
    try:
        storage.edit_customer(user, customer_id, name, phone, note)
    except RosterNotFound:
        return _customer_page(request, None, status_code=404)
    except PermissionError:
        return _roster_forbidden(request, customer)
    except RosterError as exc:
        return _customer_page(
            request,
            _customer_row(customer_id) or customer,
            error=str(exc),
            status_code=400,
            template="customer_edit.html",
            extra=edit_extra,
        )
    except Exception:
        return _customer_page(
            request,
            customer,
            error="暫時無法儲存，請稍後再試。",
            status_code=500,
            template="customer_edit.html",
            extra=edit_extra,
        )
    return RedirectResponse(f"/customers/{quote(customer_id)}?notice=saved", status_code=303)


@app.get("/customers/{customer_id}/delete", response_class=HTMLResponse)
def customer_delete_page(request: Request, customer_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    return _customer_page(request, customer, template="customer_delete.html")


@app.post("/customers/{customer_id}/delete")
async def customer_delete_submit(
    request: Request,
    customer_id: str,
    csrf: str = Form(""),
    confirm: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    customer = _customer_row(customer_id)
    if STAFF.get(user) != "super_admin":
        return _roster_forbidden(request, customer)
    if not customer:
        return _customer_page(request, None, status_code=404)
    if not _valid_csrf(request, csrf):
        return _customer_page(
            request,
            customer,
            error="請重新整理後再試。",
            status_code=400,
            template="customer_delete.html",
        )
    if confirm != ROSTER_DELETE_CONFIRM:
        return _customer_page(
            request,
            customer,
            error="請先確認再刪除。",
            status_code=400,
            template="customer_delete.html",
        )
    try:
        storage.delete_customer(user, customer_id)
    except RosterNotFound:
        return _customer_page(request, None, status_code=404)
    except PermissionError:
        return _roster_forbidden(request, customer)
    except Exception:
        return _customer_page(
            request,
            customer,
            error="暫時無法刪除，請稍後再試。",
            status_code=500,
            template="customer_delete.html",
        )
    return RedirectResponse("/customers?notice=deleted", status_code=303)


def _scan_page(
    request: Request,
    item: dict[str, Any] | None,
    *,
    error: str | None = None,
    scan_note: str | None = None,
    status_code: int = 200,
) -> Response:
    if item:
        return templates.TemplateResponse(
            request,
            "submission.html",
            _ctx(request, item=item, notice=None, error=error, scan_note=scan_note),
            status_code=status_code,
        )
    return _home_response(
        request,
        error=error,
        notice=None,
        scan_note=scan_note,
        status_code=status_code,
    )


@app.post("/scan")
async def scan_submit(
    request: Request,
    csrf: str = Form(""),
    submission_id: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    sub_id = (submission_id or "").strip()
    item = None
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sub_id):
        item = storage.get_submission(sub_id)
    else:
        sub_id = ""
    if item is None:
        sub_id = ""
    if not _valid_csrf(request, csrf):
        return _scan_page(request, item, error="請重新整理後再試。", status_code=400)
    if scan_is_limited(user):
        return _scan_page(request, item, scan_note=SCAN_LIMITED, status_code=429)
    mark_scan(user)
    try:
        result = post_scan({"requested_by": user, "submission_id": sub_id})
    except Exception:
        result = "failed"
    if result == "sent":
        if item:
            return RedirectResponse(f"/submission/{quote(item['id'])}?notice=scan-sent", status_code=303)
        return RedirectResponse("/?notice=scan-sent", status_code=303)
    if result == "unconfigured":
        return _scan_page(request, item, scan_note=SCAN_OFFLINE, status_code=200)
    return _scan_page(request, item, scan_note=SCAN_FAILED, status_code=200)


def _catalog_notice(code: str | None) -> str | None:
    return {"deleted": "已刪除此貨品。"}.get(code or "")


def _catalog_product(product_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", product_id):
        return None
    for row in storage.get_catalog_document().get("products") or []:
        if row.get("id") == product_id:
            return row
    return None


def _catalog_view(row: dict[str, Any] | None, *, with_image: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    shown = present_product(row)
    shown["image_available"] = bool(
        with_image and shown["image"] and storage.resolve_catalog_image(row) is not None
    )
    return shown


def _catalog_related(row: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for sub_id in row.get("submission_ids") or []:
        item = storage.get_submission(str(sub_id))
        if item:
            found.append(item)
    return found


def _catalog_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    query: str = "",
) -> Response:
    document = storage.get_catalog_document()
    products = list(document.get("products") or [])
    shown = filter_products(products, query)
    return templates.TemplateResponse(
        request,
        "catalog.html",
        _ctx(
            request,
            error=error,
            notice=_catalog_notice(request.query_params.get("notice")),
            rows=[present_product(row) for row in shown],
            total=len(products),
            query=query,
        ),
        status_code=status_code,
    )


def _product_page(
    request: Request,
    product: dict[str, Any] | None,
    *,
    error: str | None = None,
    status_code: int = 200,
    template: str = "product.html",
) -> Response:
    source = product
    shown = _catalog_view(product, with_image=template == "product.html") if product else None
    return templates.TemplateResponse(
        request,
        template,
        _ctx(
            request,
            product=shown,
            error=error,
            notice=_catalog_notice(request.query_params.get("notice")),
            related=_catalog_related(source) if source else [],
            catalog_delete_confirm=CATALOG_DELETE_CONFIRM,
        ),
        status_code=status_code,
    )


def _catalog_forbidden(request: Request, product: dict[str, Any] | None) -> Response:
    return _product_page(request, product, error="沒有權限。", status_code=403)


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    return _catalog_page(request, query=(request.query_params.get("q") or "").strip())


@app.get("/catalog/catalog.xlsx")
def download_catalog(request: Request) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    workbook = storage.catalog_workbook()
    filename = quote("貨品清單.xlsx")
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"catalog.xlsx\"; filename*=UTF-8''{filename}"
        },
    )


@app.get("/catalog/{product_id}", response_class=HTMLResponse)
def product_page(request: Request, product_id: str) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if not product:
        return _product_page(request, None, status_code=404)
    return _product_page(request, product)


@app.get("/catalog/{product_id}/image")
def catalog_image(request: Request, product_id: str) -> Response:
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if not product:
        return Response("找不到圖片。", status_code=404, media_type="text/plain; charset=utf-8")
    path = storage.resolve_catalog_image(product)
    if path is None:
        return Response("找不到圖片。", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@app.get("/catalog/{product_id}/delete", response_class=HTMLResponse)
def catalog_delete_page(request: Request, product_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if STAFF.get(user) != "super_admin":
        return _catalog_forbidden(request, product)
    if not product:
        return _product_page(request, None, status_code=404)
    return _product_page(request, product, template="product_delete.html")


@app.post("/catalog/{product_id}/delete")
async def catalog_delete_submit(
    request: Request,
    product_id: str,
    csrf: str = Form(""),
    confirm: str = Form(""),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if STAFF.get(user) != "super_admin":
        return _catalog_forbidden(request, product)
    if not product:
        return _product_page(request, None, status_code=404)
    if not _valid_csrf(request, csrf):
        return _product_page(
            request,
            product,
            error="請重新整理後再試。",
            status_code=400,
            template="product_delete.html",
        )
    if confirm != CATALOG_DELETE_CONFIRM:
        return _product_page(
            request,
            product,
            error="請先確認再刪除。",
            status_code=400,
            template="product_delete.html",
        )
    try:
        storage.delete_catalog_product(user, product_id)
    except CatalogNotFound:
        return _product_page(request, None, status_code=404)
    except PermissionError:
        return _catalog_forbidden(request, product)
    except CatalogError as exc:
        return _product_page(
            request,
            _catalog_product(product_id) or product,
            error=str(exc),
            status_code=400,
            template="product_delete.html",
        )
    except Exception:
        return _product_page(
            request,
            product,
            error="暫時無法刪除，請稍後再試。",
            status_code=500,
            template="product_delete.html",
        )
    return RedirectResponse("/catalog?notice=deleted", status_code=303)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)

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
from storage import Storage

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
KINDS = ["新單", "改單", "取消", "聯絡人截圖", "其他"]
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_TRIES = 8

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
        "kinds": KINDS,
        **extra,
    }


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


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    items = storage.list_submissions()
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(request, items=items, error=None, notice=request.query_params.get("notice")),
    )


@app.post("/submit")
async def submit(
    request: Request,
    kind: str = Form(""),
    note: str = Form(""),
    csrf: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not _valid_csrf(request, csrf):
        items = storage.list_submissions()
        return templates.TemplateResponse(
            request,
            "home.html",
            _ctx(request, items=items, error="請重新整理後再提交。", notice=None),
            status_code=400,
        )
    if kind not in KINDS:
        kind = "其他"
    note = (note or "").strip()[:2000]
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
        items = storage.list_submissions()
        msg = "；".join(errors) if errors else "請至少選擇一個檔案。"
        return templates.TemplateResponse(
            request,
            "home.html",
            _ctx(request, items=items, error=msg, notice=None),
            status_code=400,
        )
    try:
        sub_id = storage.create_submission(user=user, kind=kind, note=note, files=saved)
    except Exception:
        items = storage.list_submissions()
        return templates.TemplateResponse(
            request,
            "home.html",
            _ctx(request, items=items, error="儲存失敗，請稍後再試或聯絡管理員。", notice=None),
            status_code=500,
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
        _ctx(request, item=item, notice=request.query_params.get("notice")),
    )


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


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)

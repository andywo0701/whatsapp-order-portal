from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from storage import Storage

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

HK = ZoneInfo("Asia/Hong_Kong")
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


def _users() -> dict[str, str]:
    """username -> password, from secrets. Never log these values."""
    users: dict[str, str] = {}
    raw = os.environ.get("SITE_USERS", "").strip()
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, pwd = line.split(":", 1)
            name, pwd = name.strip(), pwd.strip()
            if name and pwd:
                users[name] = pwd
    user = os.environ.get("SITE_USERNAME", "").strip()
    pwd = os.environ.get("SITE_PASSWORD", "").strip()
    if user and pwd:
        users[user] = pwd
    return users


def _configured() -> bool:
    return bool(_users()) and storage.is_ready()


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
    if isinstance(user, str) and user in _users():
        return user
    return None


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _valid_csrf(request: Request, token: str | None) -> bool:
    expected = request.session.get("csrf")
    if not expected or not token:
        return False
    return hmac.compare_digest(str(expected), str(token))


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "user": _current_user(request),
        "csrf": _csrf_token(request),
        "kinds": KINDS,
        **extra,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    if not _users():
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, error="網站尚未設定登入帳號，請聯絡管理員。", configured=False),
            status_code=503,
        )
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=None, configured=True))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    ip = _client_ip(request)
    if not _valid_csrf(request, csrf):
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error="請重新整理後再試。", configured=True), status_code=400
        )
    if _rate_limited(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, error="嘗試次數過多，請 15 分鐘後再試。", configured=True),
            status_code=429,
        )
    users = _users()
    expected = users.get(username)
    ok = bool(expected) and hmac.compare_digest(expected, password)
    if not ok:
        _record_fail(ip)
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error="帳號或密碼不正確。", configured=True), status_code=401
        )
    _clear_fails(ip)
    request.session.clear()
    request.session["user"] = username
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


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)

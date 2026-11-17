from __future__ import annotations

import hmac
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from auth import (
    MIN_PASSWORD_LENGTH,
    ROLE_LABELS,
    ROLE_TITLES,
    STAFF,
    authenticate,
    can_manage,
    change_own_password,
    reset_admin_password,
)
from catalog import (
    CATALOG_DELETE_CONFIRM,
    CATALOG_EDIT_CONFIRM,
    EDIT_SLOT_LIMIT,
    CatalogError,
    CatalogNotFound,
    edit_slots,
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
from storage import ChunkUploadError, Storage, parse_iso_date, record_date_text

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
# A staff WhatsApp export must be accepted at 500MB, and a full backup is about 3GB.
# 4GB is the per-file and per-submission cap. The page sends the file in CHUNK_BYTES pieces.
MIN_FILE_BYTES = 500 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
CHUNK_BYTES = 8 * 1024 * 1024
FILE_CAP_LABEL = "4GB"
UPLOAD_LIMIT_HINT = "單檔上限 4GB。較大的檔案會分成小段送出，完成前請留在這一頁。"
_FILE_TOO_BIG = "「{name}」超過單檔 4GB 上限。"
_TOTAL_TOO_BIG = "今次上載總容量超過 4GB 上限。"
DELETE_CONFIRM = "確定刪除此提交？"
BULK_LIMIT = 200
SELECT_FIRST = "請先選擇要刪除的項目。"
BULK_MISSING = "找不到要刪除的項目。"
BULK_TOO_MANY = "一次最多刪除 200 筆，請先少選一些。"
_BULK_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
RECORD_DATE_HINT = (
    "可選，以香港日曆計算。選了單日或日期範圍時，掃描程式發布的落單 Excel 只會保留落單日期落在這段日子裡的列，"
    "範圍以外的舊紀錄不會留在這份下載。留空時，這份 Excel 可以包含截至今天的全部現有紀錄。"
)
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
        "can_manage": can_manage(user),
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
    """Optional Hong Kong calendar window stored on the manifest.

    Blank still omits record_date_start and record_date_end. A single day is
    returned as the same start and end. The scanning bot uses a chosen window
    when it builds the order Excel; this function does not build that workbook.
    Raises ValueError with a Traditional Chinese message when the chosen dates
    are not usable.
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
            record_date_hint=RECORD_DATE_HINT,
            upload_limit_hint=UPLOAD_LIMIT_HINT,
            chunk_bytes=CHUNK_BYTES,
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
    raw_length = request.headers.get("content-length", "")
    if raw_length.isdigit() and int(raw_length) > MAX_TOTAL_BYTES + 2 * 1024 * 1024:
        return _home_response(
            request,
            error=_TOTAL_TOO_BIG,
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    named = [upload for upload in files if upload.filename]
    if len(named) > MAX_FILES:
        return _home_response(
            request,
            error=f"一次最多 {MAX_FILES} 個檔案。",
            notice=None,
            status_code=400,
            form_mode=form_mode,
            form_day=record_day,
            form_start=record_date_start,
            form_end=record_date_end,
            form_purpose=echoed_purpose,
        )
    incoming_root = storage.local_root / ".incoming"
    incoming_root.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="in-", dir=incoming_root))
    saved: list[tuple[str, Path]] = []
    total = 0
    errors: list[str] = []
    try:
        for upload in named:
            name = _safe_filename(upload.filename or "")
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXT:
                errors.append(f"「{name}」檔案類型不允許。")
                continue
            dest = stage_dir / f"{len(saved):02d}.bin"
            try:
                size = await _stream_to_file(upload, dest, MAX_FILE_BYTES)
            except _TooLarge:
                errors.append(_FILE_TOO_BIG.replace("{name}", name))
                break
            if size <= 0:
                dest.unlink(missing_ok=True)
                continue
            total += size
            if total > MAX_TOTAL_BYTES:
                errors.append(_TOTAL_TOO_BIG)
                break
            saved.append((name, dest))
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
            sub_id = storage.create_submission_from_paths(
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
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return RedirectResponse(f"/submission/{quote(sub_id)}?notice=submitted", status_code=303)


class _TooLarge(Exception):
    """The streamed file passed the per-file cap before it was stored."""


async def _stream_to_file(upload: UploadFile, dest: Path, limit: int) -> int:
    """Copy an upload to disk in small blocks. The whole file is not kept in memory."""
    written = 0
    with dest.open("wb") as handle:
        while True:
            block = await upload.read(1024 * 1024)
            if not block:
                break
            if written + len(block) > limit:
                raise _TooLarge
            handle.write(block)
            written += len(block)
    if written:
        os.chmod(dest, 0o600)
    return written


def _upload_json(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _csrf_from(request: Request, form_token: str = "") -> str:
    if isinstance(form_token, str) and form_token:
        return form_token
    header = request.headers.get("x-csrf-token", "")
    return header if isinstance(header, str) else ""


async def _read_json(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body


@app.post("/upload/start")
async def upload_start(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return _upload_json("請先登入。", 401)
    body = await _read_json(request)
    if body is None:
        return _upload_json("請重新整理後再試。", 400)
    if not _valid_csrf(request, _csrf_from(request, str(body.get("csrf") or ""))):
        return _upload_json("請重新整理後再試。", 400)
    posted_purpose = str(body.get("purpose") or "").strip()
    if posted_purpose not in {"", *PURPOSE_CHOICES}:
        return _upload_json("請選擇上載用途。", 400)
    echoed = posted_purpose if posted_purpose in PURPOSE_CHOICES else PURPOSE_AUTO
    try:
        record_start, record_end = parse_record_dates(
            str(body.get("record_mode") or ""),
            str(body.get("record_day") or ""),
            str(body.get("record_date_start") or ""),
            str(body.get("record_date_end") or ""),
        )
    except ValueError as exc:
        return _upload_json(str(exc), 400)
    raw_files = body.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return _upload_json("請至少選擇一個檔案。", 400)
    described: list[dict[str, Any]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            return _upload_json("檔案資料不正確。", 400)
        name = _safe_filename(str(item.get("name") or ""))
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            return _upload_json(f"「{name}」檔案類型不允許。", 400)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            return _upload_json("檔案資料不正確。", 400)
        described.append({"name": name, "size": size})
    try:
        chosen_purpose = detect_purpose(echoed, [item["name"] for item in described])
    except ValueError as exc:
        return _upload_json(str(exc), 400)
    note = str(body.get("note") or "").strip()[:2000]
    try:
        status = storage.begin_chunk_upload(
            user,
            note=note,
            kind="",
            purpose=chosen_purpose,
            record_start=record_start,
            record_end=record_end,
            files=described,
            chunk_bytes=CHUNK_BYTES,
            max_file_bytes=MAX_FILE_BYTES,
            max_total_bytes=MAX_TOTAL_BYTES,
            max_files=MAX_FILES,
            file_too_big=_FILE_TOO_BIG,
            total_too_big=_TOTAL_TOO_BIG,
        )
    except ChunkUploadError as exc:
        return _upload_json(str(exc), 400)
    return JSONResponse(status)


@app.get("/upload/{upload_id}")
async def upload_status(request: Request, upload_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return _upload_json("請先登入。", 401)
    if not _valid_csrf(request, _csrf_from(request)):
        return _upload_json("請重新整理後再試。", 400)
    try:
        return JSONResponse(storage.chunk_status(user, upload_id))
    except ChunkUploadError as exc:
        return _upload_json(str(exc), 404)


@app.post("/upload/{upload_id}/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str,
    csrf: str = Form(""),
    file_index: str = Form(""),
    index: str = Form(""),
    chunk: UploadFile = File(default=None),
) -> Response:
    user = _current_user(request)
    if not user:
        return _upload_json("請先登入。", 401)
    if not _valid_csrf(request, _csrf_from(request, csrf)):
        return _upload_json("請重新整理後再試。", 400)
    raw_length = request.headers.get("content-length", "")
    if raw_length.isdigit() and int(raw_length) > CHUNK_BYTES + 256 * 1024:
        return _upload_json("這一段太大，請再試。", 400)
    if chunk is None:
        return _upload_json("這一段不完整，請再試。", 400)
    parsed_file = _small_int(file_index)
    parsed_index = _small_int(index)
    if parsed_file is None or parsed_index is None:
        return _upload_json("這一段不屬於這個檔案。", 400)
    try:
        plan = storage.chunk_write_plan(user, upload_id, parsed_file, parsed_index)
        await _write_chunk(chunk, plan["path"], plan["offset"], plan["expected"])
        status = storage.mark_chunk(user, upload_id, parsed_file, parsed_index)
    except ChunkUploadError as exc:
        return _upload_json(str(exc), 400)
    except _TooLarge:
        return _upload_json("這一段太大，請再試。", 400)
    return JSONResponse(status)


@app.post("/upload/{upload_id}/complete")
async def upload_complete(request: Request, upload_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return _upload_json("請先登入。", 401)
    body = await _read_json(request)
    token = _csrf_from(request, str((body or {}).get("csrf") or ""))
    if not _valid_csrf(request, token):
        return _upload_json("請重新整理後再試。", 400)
    try:
        sub_id = storage.finish_chunk_upload(user, upload_id)
    except ChunkUploadError as exc:
        return _upload_json(str(exc), 400)
    except Exception:
        return _upload_json("儲存失敗，請稍後再試或聯絡管理員。", 500)
    return JSONResponse(
        {"ok": True, "id": sub_id, "redirect": f"/submission/{quote(sub_id)}?notice=submitted"}
    )


def _small_int(value: str) -> int | None:
    if not isinstance(value, str) or not value.isdigit():
        return None
    number = int(value)
    if number > 1_000_000:
        return None
    return number


async def _write_chunk(upload: UploadFile, dest: Path, offset: int, expected: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.touch()
        os.chmod(dest, 0o600)
    written = 0
    with dest.open("r+b") as handle:
        handle.seek(offset)
        while written < expected:
            block = await upload.read(min(1024 * 1024, expected - written))
            if not block:
                raise ChunkUploadError("這一段不完整，請再試。")
            if written + len(block) > expected:
                raise _TooLarge
            handle.write(block)
            written += len(block)
        extra = await upload.read(1)
        if extra:
            raise _TooLarge
    os.chmod(dest, 0o600)


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
    if not can_manage(user):
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
    if not can_manage(user):
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


def _staff_rows(actor: str) -> list[dict[str, Any]]:
    storage.refresh_password_records()
    rows: list[dict[str, Any]] = []
    for name, role in STAFF.items():
        if role != "admin" or name == actor:
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
    actor = _current_user(request) or ""
    staff = _staff_rows(actor) if can_manage(actor) else []
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
    if not can_manage(user):
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
        "bulk-deleted": "已刪除所選顧客。",
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
    if not can_manage(user):
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
    if not can_manage(user):
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
    if not can_manage(user):
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
    if not can_manage(user):
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
    if not can_manage(user):
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
    if not can_manage(user):
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
    return {
        "deleted": "已刪除此貨品。",
        "bulk-deleted": "已刪除所選貨品。",
        "saved": "已儲存此貨品。",
    }.get(code or "")


def _form_text(form: Any, key: str) -> str:
    value = form.get(key)
    if isinstance(value, str):
        return value
    return ""


def _posted_options(form: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(EDIT_SLOT_LIMIT):
        clear = _form_text(form, f"option_clear_{index}")
        rows.append(
            {
                "size": _form_text(form, f"option_size_{index}"),
                "color": _form_text(form, f"option_color_{index}"),
                "other": _form_text(form, f"option_other_{index}"),
                "price": _form_text(form, f"option_price_{index}"),
                "prev_size": _form_text(form, f"option_prev_size_{index}"),
                "prev_color": _form_text(form, f"option_prev_color_{index}"),
                "prev_other": _form_text(form, f"option_prev_other_{index}"),
                "clear": clear in {"1", "on", "yes", "清除"},
            }
        )
    return rows


def _posted_slots(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    for item in rows:
        if not item.get("clear") and not any(
            str(item.get(key) or "")
            for key in ("size", "color", "other", "price", "prev_size", "prev_color", "prev_other")
        ):
            continue
        slots.append(
            {
                "size": str(item.get("size") or ""),
                "color": str(item.get("color") or ""),
                "other": str(item.get("other") or ""),
                "price": str(item.get("price") or ""),
                "prev_size": str(item.get("prev_size") or ""),
                "prev_color": str(item.get("prev_color") or ""),
                "prev_other": str(item.get("prev_other") or ""),
                "clear": "1" if item.get("clear") else "",
            }
        )
    if not slots:
        slots = edit_slots([])
    return slots


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
    extra: dict[str, Any] | None = None,
) -> Response:
    source = product
    shown = _catalog_view(product, with_image=template in {"product.html", "product_edit.html"}) if product else None
    payload: dict[str, Any] = {
        "product": shown,
        "error": error,
        "notice": _catalog_notice(request.query_params.get("notice")),
        "related": _catalog_related(source) if source else [],
        "catalog_delete_confirm": CATALOG_DELETE_CONFIRM,
        "catalog_edit_confirm": CATALOG_EDIT_CONFIRM,
    }
    if extra:
        payload.update(extra)
    if template == "product_edit.html" and shown is not None and "edit_slots" not in payload:
        payload["edit_name"] = shown["name"]
        payload["edit_price"] = shown["price"]
        payload["edit_note"] = shown["note"]
        payload["edit_slots"] = edit_slots(shown["options"])
    return templates.TemplateResponse(
        request,
        template,
        _ctx(request, **payload),
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


@app.get("/catalog/{product_id}/edit", response_class=HTMLResponse)
def catalog_edit_page(request: Request, product_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if not can_manage(user):
        return _catalog_forbidden(request, product)
    if not product:
        return _product_page(request, None, status_code=404)
    return _product_page(request, product, template="product_edit.html")


@app.post("/catalog/{product_id}/edit")
async def catalog_edit_submit(request: Request, product_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if not can_manage(user):
        return _catalog_forbidden(request, product)
    if not product:
        return _product_page(request, None, status_code=404)
    form = await request.form()
    posted = _posted_options(form)
    edit_extra = {
        "edit_name": _form_text(form, "name"),
        "edit_price": _form_text(form, "price"),
        "edit_note": _form_text(form, "note"),
        "edit_slots": _posted_slots(posted),
    }
    if not _valid_csrf(request, _form_text(form, "csrf")):
        return _product_page(
            request,
            product,
            error="請重新整理後再試。",
            status_code=400,
            template="product_edit.html",
            extra=edit_extra,
        )
    if _form_text(form, "confirm") != CATALOG_EDIT_CONFIRM:
        return _product_page(
            request,
            product,
            error="請先確認再儲存。",
            status_code=400,
            template="product_edit.html",
            extra=edit_extra,
        )
    try:
        storage.edit_catalog_product(
            user,
            product_id,
            edit_extra["edit_name"],
            edit_extra["edit_price"],
            edit_extra["edit_note"],
            posted,
        )
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
            template="product_edit.html",
            extra=edit_extra,
        )
    except Exception:
        return _product_page(
            request,
            product,
            error="暫時無法儲存，請稍後再試。",
            status_code=500,
            template="product_edit.html",
            extra=edit_extra,
        )
    return RedirectResponse(f"/catalog/{quote(product_id)}?notice=saved", status_code=303)


@app.get("/catalog/{product_id}/delete", response_class=HTMLResponse)
def catalog_delete_page(request: Request, product_id: str) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    product = _catalog_product(product_id)
    if not can_manage(user):
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
    if not can_manage(user):
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


def bulk_phrase(count: int) -> str:
    return f"確定刪除已選的 {count} 筆？"


def _clean_bulk_ids(values: list[Any]) -> tuple[list[str], bool]:
    chosen: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not _BULK_ID.fullmatch(text) or text in seen:
            continue
        if len(chosen) >= BULK_LIMIT:
            return chosen, True
        seen.add(text)
        chosen.append(text)
    return chosen, False


def _form_id_list(form: Any) -> list[Any]:
    getlist = getattr(form, "getlist", None)
    if not callable(getlist):
        return []
    return list(getlist("ids"))


def _bulk_page(
    request: Request,
    *,
    action: str,
    cancel: str,
    hint: str,
    items: list[dict[str, str]],
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    phrase = bulk_phrase(len(items))
    return templates.TemplateResponse(
        request,
        "bulk_delete.html",
        _ctx(
            request,
            action=action,
            cancel=cancel,
            hint=hint,
            items=items,
            phrase=phrase,
            error=error,
        ),
        status_code=status_code,
    )


def _submission_matches(ids: list[str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for sub_id in ids:
        item = storage.get_submission(sub_id)
        if not item or item.get("deleted"):
            continue
        kind = str(item.get("kind") or "提交")
        when = str(item.get("submitted_display") or sub_id)
        found.append({"id": str(item["id"]), "label": f"{kind} · {when}"})
    return found


def _catalog_matches(ids: list[str]) -> list[dict[str, str]]:
    products = storage.get_catalog_document().get("products") or []
    by_id = {
        row["id"]: row
        for row in products
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    found: list[dict[str, str]] = []
    for product_id in ids:
        row = by_id.get(product_id)
        if not row:
            continue
        shown = present_product(row)
        found.append({"id": str(shown["id"]), "label": str(shown["name"])})
    return found


def _customer_matches(ids: list[str]) -> list[dict[str, str]]:
    customers = storage.get_roster_document().get("customers") or []
    by_id = {
        row["id"]: row
        for row in customers
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    found: list[dict[str, str]] = []
    for customer_id in ids:
        row = by_id.get(customer_id)
        if not row:
            continue
        shown = present_customer(row)
        found.append({"id": str(shown["id"]), "label": str(shown["name_display"])})
    return found


@app.post("/submissions/bulk-delete")
async def bulk_delete_submissions(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not can_manage(user):
        return _home_response(request, error="沒有權限。", notice=None, status_code=403)
    form = await request.form()
    if not _valid_csrf(request, _form_text(form, "csrf")):
        return _home_response(request, error="請重新整理後再試。", notice=None, status_code=400)
    ids, too_many = _clean_bulk_ids(_form_id_list(form))
    if too_many:
        return _home_response(request, error=BULK_TOO_MANY, notice=None, status_code=400)
    if not ids:
        return _home_response(request, error=SELECT_FIRST, notice=None, status_code=400)
    items = _submission_matches(ids)
    if not items:
        return _home_response(request, error=BULK_MISSING, notice=None, status_code=400)
    phrase = bulk_phrase(len(items))
    confirm = _form_text(form, "confirm")
    hint = "確認之後，這些提交會標成已刪除，並從近期列表隱藏。上載檔案不會被清掉。"
    if confirm != phrase:
        return _bulk_page(
            request,
            action="/submissions/bulk-delete",
            cancel="/",
            hint=hint,
            items=items,
            error="請先確認再刪除。" if confirm else None,
            status_code=400 if confirm else 200,
        )
    deleted = 0
    try:
        for item in items:
            updated = storage.soft_delete_submission(item["id"], user)
            if updated:
                deleted += 1
    except PermissionError:
        return _home_response(request, error="沒有權限。", notice=None, status_code=403)
    except Exception:
        return _bulk_page(
            request,
            action="/submissions/bulk-delete",
            cancel="/",
            hint=hint,
            items=items,
            error="暫時無法刪除，請稍後再試。",
            status_code=500,
        )
    if deleted == 0:
        return _home_response(request, error=BULK_MISSING, notice=None, status_code=400)
    return RedirectResponse("/?notice=bulk-deleted", status_code=303)


@app.post("/catalog/bulk-delete")
async def bulk_delete_catalog(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    query = _form_text(form, "q")
    if not can_manage(user):
        return _catalog_page(request, error="沒有權限。", status_code=403, query=query)
    if not _valid_csrf(request, _form_text(form, "csrf")):
        return _catalog_page(request, error="請重新整理後再試。", status_code=400, query=query)
    ids, too_many = _clean_bulk_ids(_form_id_list(form))
    if too_many:
        return _catalog_page(request, error=BULK_TOO_MANY, status_code=400, query=query)
    if not ids:
        return _catalog_page(request, error=SELECT_FIRST, status_code=400, query=query)
    items = _catalog_matches(ids)
    if not items:
        return _catalog_page(request, error=BULK_MISSING, status_code=400, query=query)
    phrase = bulk_phrase(len(items))
    confirm = _form_text(form, "confirm")
    hint = "確認之後，這些貨品會從清單移除。之後的掃描如果再用同一個編號或名稱發布，也不會把它們加回來。已上載的檔案不會被清掉。"
    if confirm != phrase:
        return _bulk_page(
            request,
            action="/catalog/bulk-delete",
            cancel="/catalog",
            hint=hint,
            items=items,
            error="請先確認再刪除。" if confirm else None,
            status_code=400 if confirm else 200,
        )
    try:
        storage.delete_catalog_products(user, [item["id"] for item in items])
    except PermissionError:
        return _catalog_page(request, error="沒有權限。", status_code=403, query=query)
    except CatalogNotFound:
        return _catalog_page(request, error=BULK_MISSING, status_code=400, query=query)
    except Exception:
        return _bulk_page(
            request,
            action="/catalog/bulk-delete",
            cancel="/catalog",
            hint=hint,
            items=items,
            error="暫時無法刪除，請稍後再試。",
            status_code=500,
        )
    location = "/catalog?notice=bulk-deleted"
    if query:
        location = f"{location}&q={quote(query)}"
    return RedirectResponse(location, status_code=303)


@app.post("/customers/bulk-delete")
async def bulk_delete_customers(request: Request) -> Response:
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    query = _form_text(form, "q")
    which = _form_text(form, "filter") or "all"
    if not can_manage(user):
        return _customers_page(request, error="沒有權限。", status_code=403, query=query, which=which)
    if not _valid_csrf(request, _form_text(form, "csrf")):
        return _customers_page(request, error="請重新整理後再試。", status_code=400, query=query, which=which)
    ids, too_many = _clean_bulk_ids(_form_id_list(form))
    if too_many:
        return _customers_page(request, error=BULK_TOO_MANY, status_code=400, query=query, which=which)
    if not ids:
        return _customers_page(request, error=SELECT_FIRST, status_code=400, query=query, which=which)
    items = _customer_matches(ids)
    if not items:
        return _customers_page(request, error=BULK_MISSING, status_code=400, query=query, which=which)
    phrase = bulk_phrase(len(items))
    confirm = _form_text(form, "confirm")
    hint = "確認之後，這些顧客會從名冊移除。之後的掃描如果再用同一個編號發布，也不會把他們加回來。"
    if confirm != phrase:
        return _bulk_page(
            request,
            action="/customers/bulk-delete",
            cancel="/customers",
            hint=hint,
            items=items,
            error="請先確認再刪除。" if confirm else None,
            status_code=400 if confirm else 200,
        )
    try:
        storage.delete_customers(user, [item["id"] for item in items])
    except PermissionError:
        return _customers_page(request, error="沒有權限。", status_code=403, query=query, which=which)
    except RosterNotFound:
        return _customers_page(request, error=BULK_MISSING, status_code=400, query=query, which=which)
    except Exception:
        return _bulk_page(
            request,
            action="/customers/bulk-delete",
            cancel="/customers",
            hint=hint,
            items=items,
            error="暫時無法刪除，請稍後再試。",
            status_code=500,
        )
    location = "/customers?notice=bulk-deleted"
    if query or (which and which != "all"):
        location = f"{location}&filter={quote(which or 'all')}&q={quote(query)}"
    return RedirectResponse(location, status_code=303)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)

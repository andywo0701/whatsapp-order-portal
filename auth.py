"""Staff roster and password hashing. Never log or store plaintext passwords."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any

# Usernames are fixed. Passwords are never kept in this module.
STAFF: dict[str, str] = {
    "Andy": "super_admin",
    "Debbie": "admin",
    "Taya": "admin",
    "Katherine": "admin",
}
ROLE_LABELS = {
    "super_admin": "超級管理員",
    "admin": "管理員",
}
ROLE_TITLES = {
    "super_admin": "Super Admin",
    "admin": "Admin",
}
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
# OWASP PBKDF2-HMAC-SHA256 guidance. The count is not a secret.
PBKDF2_ITERATIONS = 600_000


def role_of(username: str) -> str | None:
    return STAFF.get(username)


def secrets_equal(left: str, right: str) -> bool:
    a = left.encode("utf-8")
    b = right.encode("utf-8")
    if len(a) != len(b):
        hmac.compare_digest(a, a)
        return False
    return hmac.compare_digest(a, b)


def hash_password(password: str, generation: int) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32
    )
    return {
        "algo": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "dklen": 32,
        "salt": _b64(salt),
        "hash": _b64(digest),
        "generation": int(generation),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def verify_password(password: str, record: dict[str, Any] | None) -> bool:
    if not record or record.get("algo") != "pbkdf2_sha256":
        return False
    try:
        salt = _unb64(str(record["salt"]))
        expected = _unb64(str(record["hash"]))
        iterations = int(record["iterations"])
        dklen = int(record.get("dklen") or len(expected))
    except (KeyError, TypeError, ValueError):
        return False
    if iterations < 1 or iterations > 2_000_000 or dklen < 16 or dklen > 64:
        return False
    if len(expected) != dklen:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=dklen
    )
    return hmac.compare_digest(digest, expected)


def bootstrap_password(username: str) -> str | None:
    """Env password used only when no hash is stored yet. Never log the result."""
    if username not in STAFF:
        return None
    found: dict[str, str] = {}
    raw = os.environ.get("SITE_USERS", "")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, pwd = line.split(":", 1)
        name, pwd = name.strip(), pwd.strip()
        if name in STAFF and pwd:
            found[name] = pwd
    single_name = os.environ.get("SITE_USERNAME", "").strip()
    single_pwd = os.environ.get("SITE_PASSWORD", "").strip()
    if single_name in STAFF and single_pwd:
        found[single_name] = single_pwd
    per_user = os.environ.get(f"SITE_PASSWORD_{username.upper()}", "").strip()
    if per_user:
        found[username] = per_user
    return found.get(username)


def authenticate(
    username: str, password: str, record: dict[str, Any] | None
) -> tuple[bool, dict[str, Any] | None]:
    """Return (ok, record_to_store). A record is returned only for first-time bootstrap."""
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False, None
    if username not in STAFF:
        _burn(password)
        return False, None
    if record and record.get("hash"):
        return verify_password(password, record), None
    boot = bootstrap_password(username)
    if not boot:
        _burn(password)
        return False, None
    if not secrets_equal(boot, password):
        return False, None
    return True, hash_password(password, generation=1)


def password_shape_error(new: str, confirm: str) -> str | None:
    if not isinstance(new, str) or not isinstance(confirm, str):
        return "請輸入新密碼。"
    if len(new) < MIN_PASSWORD_LENGTH or len(new) > MAX_PASSWORD_LENGTH:
        return f"新密碼至少要 {MIN_PASSWORD_LENGTH} 個字元，最多 {MAX_PASSWORD_LENGTH} 個字元。"
    if new != confirm:
        return "兩次輸入的新密碼不一致。"
    return None


def change_own_password(
    username: str,
    current: str,
    new: str,
    confirm: str,
    record: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    if username not in STAFF:
        return "沒有權限。", None
    shape = password_shape_error(new, confirm)
    if shape:
        return shape, None
    ok, promoted = authenticate(username, current, record)
    if not ok:
        return "目前密碼不正確。", None
    baseline = promoted if promoted is not None else record
    if baseline and verify_password(new, baseline):
        return "請換一個與目前不同的密碼。", None
    generation = 2 if promoted is not None else int((record or {}).get("generation") or 1) + 1
    return None, hash_password(new, generation=generation)


def reset_admin_password(
    actor: str,
    target: str,
    new: str,
    confirm: str,
    existing: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    if STAFF.get(actor) != "super_admin":
        return "沒有權限。", None
    if target not in STAFF or STAFF.get(target) != "admin" or target == actor:
        return "只能為其他管理員設定密碼。", None
    shape = password_shape_error(new, confirm)
    if shape:
        return shape, None
    generation = int((existing or {}).get("generation") or 0) + 1
    return None, hash_password(new, generation=generation)


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    import base64

    return base64.b64decode(value.encode("ascii"), validate=True)


def _burn(password: str) -> None:
    """Spend a comparable amount of time when the account cannot authenticate."""
    salt = b"wa-portal-dummy-salt"
    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)

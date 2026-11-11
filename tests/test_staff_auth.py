"""Staff login, password change, and reset. Uses generated passwords, not real ones."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-test-"))
os.environ["LOCAL_DATA_DIR"] = str(_TMP)
os.environ["HTTPS_ONLY"] = "false"
os.environ["SESSION_SECRET"] = secrets.token_hex(16)
os.environ.pop("HF_TOKEN", None)
os.environ.pop("HUGGINGFACE_HUB_TOKEN", None)
os.environ.pop("INBOX_DATASET", None)
os.environ.pop("HF_DATASET", None)
os.environ["SITE_USERS"] = ""
os.environ.pop("SITE_USERNAME", None)
os.environ.pop("SITE_PASSWORD", None)

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from auth import (  # noqa: E402
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PBKDF2_ITERATIONS,
    STAFF,
    authenticate,
    hash_password,
    verify_password,
)

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def _password() -> str:
    return "pw-" + secrets.token_urlsafe(12)


def _csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    if not match:
        raise AssertionError("csrf missing")
    return match.group(1)


def _clear_bootstrap() -> None:
    os.environ["SITE_USERS"] = ""
    os.environ.pop("SITE_USERNAME", None)
    os.environ.pop("SITE_PASSWORD", None)
    for name in STAFF:
        os.environ.pop(f"SITE_PASSWORD_{name.upper()}", None)


class StaffAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._login_attempts.clear()
        path = app_module.storage._accounts_file()
        if path.exists():
            path.unlink()
        app_module.storage._password_records = None
        _clear_bootstrap()

    def test_roster_and_hash_constant(self) -> None:
        self.assertEqual(
            list(STAFF),
            ["Andy", "Debbie", "Taya", "Katherine"],
        )
        self.assertEqual(
            set(STAFF.values()),
            {"super_admin", "admin"},
        )
        self.assertGreaterEqual(PBKDF2_ITERATIONS, 600_000)
        self.assertGreaterEqual(MIN_PASSWORD_LENGTH, 10)
        self.assertLessEqual(MAX_PASSWORD_LENGTH, 128)

    def test_hash_verify_omits_plaintext(self) -> None:
        secret = _password()
        record = hash_password(secret, generation=1)
        self.assertNotIn("password", record)
        self.assertNotIn(secret, json.dumps(record))
        self.assertTrue(verify_password(secret, record))
        self.assertFalse(verify_password(_password(), record))
        with self.assertRaises(ValueError):
            app_module.storage._clean_password_record({**record, "password": secret})

    def test_stored_hash_beats_env(self) -> None:
        original = _password()
        os.environ["SITE_USERS"] = f"Andy:{original}"
        ok, promoted = authenticate("Andy", original, None)
        self.assertTrue(ok)
        self.assertIsNotNone(promoted)
        os.environ["SITE_USERS"] = f"Andy:{_password()}"
        ok_env, promoted_env = authenticate("Andy", os.environ["SITE_USERS"].split(":", 1)[1], promoted)
        self.assertFalse(ok_env)
        self.assertIsNone(promoted_env)
        ok_old, _ = authenticate("Andy", original, promoted)
        self.assertTrue(ok_old)
        ok_unknown, _ = authenticate("andy", original, None)
        self.assertFalse(ok_unknown)

    def test_login_change_and_reset(self) -> None:
        andy_password = _password()
        changed = _password()
        reset_to = _password()
        debbie_changed = _password()
        os.environ["SITE_PASSWORD_ANDY"] = andy_password

        andy = TestClient(app_module.app)
        failed = self._login(andy, "Debbie", _password())
        self.assertEqual(failed.status_code, 401)
        self.assertNotIn(os.environ["SITE_PASSWORD_ANDY"], failed.text)

        first = self._login(andy, "Andy", andy_password)
        self.assertEqual(first.status_code, 303)
        home = andy.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("歡迎回來", home.text)
        self.assertIn("超級管理員", home.text)
        self.assertIn("Super Admin", home.text)
        self.assertNotIn(andy_password, home.text)

        stored = json.loads(app_module.storage._accounts_file().read_text(encoding="utf-8"))
        self.assertNotIn(andy_password, json.dumps(stored))
        self.assertEqual(stored["users"]["Andy"]["algo"], "pbkdf2_sha256")
        self.assertNotIn("password", stored["users"]["Andy"])

        os.environ["SITE_PASSWORD_ANDY"] = _password()
        stale = TestClient(app_module.app)
        self.assertEqual(self._login(stale, "Andy", os.environ["SITE_PASSWORD_ANDY"]).status_code, 401)
        self.assertEqual(self._login(stale, "Andy", andy_password).status_code, 303)

        account = andy.get("/account")
        self.assertIn("Debbie", account.text)
        self.assertIn("Taya", account.text)
        self.assertIn("Katherine", account.text)
        self.assertIn("尚未設定", account.text)
        self.assertIn("幫同事設定密碼", account.text)
        self.assertNotIn(andy_password, account.text)
        changed_page = andy.post(
            "/account/password",
            data={
                "csrf": _csrf(account.text),
                "current_password": andy_password,
                "new_password": changed,
                "confirm_password": changed,
            },
            follow_redirects=False,
        )
        self.assertEqual(changed_page.status_code, 303)
        self.assertNotIn(changed, changed_page.headers["location"])
        self.assertIn("密碼已更新", andy.get(changed_page.headers["location"]).text)
        self.assertNotIn(changed, json.dumps(json.loads(app_module.storage._accounts_file().read_text(encoding="utf-8"))))

        again = TestClient(app_module.app)
        self.assertEqual(self._login(again, "Andy", andy_password).status_code, 401)
        self.assertEqual(self._login(again, "Andy", changed).status_code, 303)

        short = andy.post(
            "/account/password",
            data={
                "csrf": _csrf(andy.get("/account").text),
                "current_password": changed,
                "new_password": "short",
                "confirm_password": "short",
            },
        )
        self.assertEqual(short.status_code, 400)
        self.assertIn("至少要", short.text)
        self.assertNotIn("short", short.text)

        mismatch = andy.post(
            "/account/password",
            data={
                "csrf": _csrf(short.text),
                "current_password": changed,
                "new_password": _password(),
                "confirm_password": _password(),
            },
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("不一致", mismatch.text)

        reset = andy.post(
            "/account/reset",
            data={
                "csrf": _csrf(andy.get("/account").text),
                "target": "Debbie",
                "new_password": reset_to,
                "confirm_password": reset_to,
            },
            follow_redirects=False,
        )
        self.assertEqual(reset.status_code, 303)
        self.assertNotIn(reset_to, reset.headers["location"])
        saved = json.dumps(json.loads(app_module.storage._accounts_file().read_text(encoding="utf-8")))
        self.assertNotIn(reset_to, saved)
        reset_page = andy.get(reset.headers["location"])
        self.assertIn("已為同事設定新密碼", reset_page.text)
        self.assertIn("已設定密碼", reset_page.text)

        debbie = TestClient(app_module.app)
        self.assertEqual(self._login(debbie, "Debbie", reset_to).status_code, 303)
        debbie_account = debbie.get("/account")
        self.assertIn("管理員", debbie_account.text)
        self.assertIn("Admin", debbie_account.text)
        self.assertNotIn("幫同事設定密碼", debbie_account.text)
        forbidden = debbie.post(
            "/account/reset",
            data={
                "csrf": _csrf(debbie.get("/account").text),
                "target": "Taya",
                "new_password": _password(),
                "confirm_password": _password(),
            },
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertIn("沒有權限", forbidden.text)

        self_reset = andy.post(
            "/account/reset",
            data={
                "csrf": _csrf(andy.get("/account").text),
                "target": "Andy",
                "new_password": _password(),
                "confirm_password": _password(),
            },
        )
        self.assertEqual(self_reset.status_code, 400)
        self.assertIn("只能為其他管理員設定密碼", self_reset.text)

        moved = debbie.post(
            "/account/password",
            data={
                "csrf": _csrf(debbie.get("/account").text),
                "current_password": reset_to,
                "new_password": debbie_changed,
                "confirm_password": debbie_changed,
            },
            follow_redirects=False,
        )
        self.assertEqual(moved.status_code, 303)
        fresh = TestClient(app_module.app)
        self.assertEqual(self._login(fresh, "Debbie", reset_to).status_code, 401)
        self.assertEqual(self._login(fresh, "Debbie", debbie_changed).status_code, 303)
        self.assertEqual(self._login(TestClient(app_module.app), "Katherine", _password()).status_code, 401)

    def test_csrf_rate_limit_and_health(self) -> None:
        password = _password()
        os.environ["SITE_USERS"] = f"Andy:{password}"
        client = TestClient(app_module.app)
        page = client.get("/login")
        self.assertIn("歡迎回來", page.text)
        self.assertNotIn("#1f4e79", Path("static/style.css").read_text(encoding="utf-8"))
        bad = client.post(
            "/login",
            data={"username": "Andy", "password": password, "csrf": "not-the-token"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn(password, bad.text)
        for _ in range(8):
            client.post(
                "/login",
                data={"username": "Eve", "password": _password(), "csrf": _csrf(client.get("/login").text)},
            )
        limited = client.post(
            "/login",
            data={"username": "Andy", "password": password, "csrf": _csrf(client.get("/login").text)},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(client.get("/healthz").json(), {"ok": "true"})

    def _login(self, client: TestClient, username: str, password: str):
        page = client.get("/login")
        return client.post(
            "/login",
            data={"username": username, "password": password, "csrf": _csrf(page.text)},
            follow_redirects=False,
        )


if __name__ == "__main__":
    unittest.main()

"""Chunked staff uploads. Uses tiny pieces, not a multi-gigabyte fixture."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-chunk-"))
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
from auth import STAFF  # noqa: E402
from storage import CHUNK_TTL_SECONDS  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
HALF_GB = 500 * 1024 * 1024


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


class ChunkUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._login_attempts.clear()
        storage = app_module.storage
        storage.repo_id = ""
        storage.token = ""
        storage._api = None
        storage._ensured = False
        storage._password_records = None
        accounts = storage._accounts_file()
        if accounts.exists():
            accounts.unlink()
        _clear_bootstrap()
        self.passwords = {name: _password() for name in STAFF}
        os.environ["SITE_USERS"] = "\n".join(f"{name}:{self.passwords[name]}" for name in STAFF)

    def tearDown(self) -> None:
        _clear_bootstrap()
        app_module.storage.repo_id = ""
        app_module.storage.token = ""
        app_module.storage._api = None
        app_module.storage._ensured = False

    def test_page_states_the_4gb_cap_and_accepts_500mb(self) -> None:
        self.assertGreaterEqual(app_module.MAX_FILE_BYTES, HALF_GB)
        self.assertGreaterEqual(app_module.MAX_TOTAL_BYTES, HALF_GB)
        self.assertEqual(app_module.MIN_FILE_BYTES, HALF_GB)
        self.assertEqual(app_module.MAX_FILE_BYTES, 4 * 1024 * 1024 * 1024)
        self.assertEqual(app_module.CHUNK_BYTES, 2 * 1024 * 1024)
        self.assertLessEqual(app_module.CHUNK_BYTES, 2 * 1024 * 1024)
        andy = self._login("Andy")
        home = andy.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(app_module.UPLOAD_LIMIT_HINT, home.text)
        self.assertNotIn("40MB", home.text)
        self.assertNotIn("80MB", home.text)
        self.assertIn("upload.js", home.text)
        self.assertIn(f'data-chunk-bytes="{app_module.CHUNK_BYTES}"', home.text)
        csrf = _csrf(home.text)
        started = andy.post(
            "/upload/start",
            json={
                "csrf": csrf,
                "purpose": "orders",
                "record_mode": "",
                "note": "",
                "files": [{"name": "backup.zip", "size": HALF_GB}],
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        body = started.json()
        upload_id = body["id"]
        parts = app_module.storage.local_root / ".chunk-uploads" / upload_id / "parts"
        self.assertEqual(list(parts.iterdir()), [])
        self.assertLess(sum(path.stat().st_size for path in parts.parent.rglob("*") if path.is_file()), 100_000)
        too_big = andy.post(
            "/upload/start",
            json={
                "csrf": csrf,
                "purpose": "orders",
                "record_mode": "",
                "files": [{"name": "backup.zip", "size": app_module.MAX_FILE_BYTES + 1}],
            },
        )
        self.assertEqual(too_big.status_code, 400, too_big.text)
        self.assertIn("超過單檔 4GB 上限", too_big.text)
        self.assertNotIn("40MB", too_big.text)
        self.assertNotIn("80MB", too_big.text)

    def test_direct_post_rejects_an_oversize_file_without_keeping_it(self) -> None:
        andy = self._login("Andy")
        home = andy.get("/")
        csrf = _csrf(home.text)
        before = {path.name for path in app_module.storage.local_root.iterdir()}
        with patch.object(app_module, "MAX_FILE_BYTES", 8):
            rejected = andy.post(
                "/submit",
                data={"csrf": csrf, "purpose": "orders", "record_mode": "", "note": ""},
                files={"files": ("notes.txt", b"0123456789abcdef", "text/plain")},
            )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("超過單檔 4GB 上限", rejected.text)
        self.assertNotIn("40MB", rejected.text)
        self.assertNotIn("80MB", rejected.text)
        created = {path.name for path in app_module.storage.local_root.iterdir()} - before
        self.assertFalse(any(name[:2].isdigit() for name in created), created)

    def test_chunks_assemble_in_any_order_and_keep_the_manifest(self) -> None:
        andy = self._login("Andy")
        home = andy.get("/")
        csrf = _csrf(home.text)
        payload = b"abcdefghij"
        with patch.object(app_module, "CHUNK_BYTES", 4):
            started = andy.post(
                "/upload/start",
                json={
                    "csrf": csrf,
                    "purpose": "orders",
                    "record_mode": "day",
                    "record_day": "2026-11-02",
                    "note": "分段測試",
                    "files": [{"name": "../../secret.txt", "size": len(payload)}],
                },
            )
        self.assertEqual(started.status_code, 200, started.text)
        upload_id = started.json()["id"]
        outside = app_module.storage.local_root.parent / "secret.txt"
        self.assertFalse(outside.exists())
        pieces = [(2, payload[8:]), (0, payload[:4]), (1, payload[4:8])]
        early = andy.post(f"/upload/{upload_id}/complete", json={"csrf": csrf})
        self.assertEqual(early.status_code, 400, early.text)
        self.assertIn("尚未完成", early.text)
        for index, blob in pieces:
            sent = andy.post(
                f"/upload/{upload_id}/chunk",
                data={"csrf": csrf, "file_index": "0", "index": str(index)},
                files={"chunk": ("chunk", blob, "application/octet-stream")},
            )
            self.assertEqual(sent.status_code, 200, sent.text)
        status = andy.get(f"/upload/{upload_id}", headers={"X-CSRF-Token": csrf})
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["files"][0]["received"], [0, 1, 2])
        part_dir = app_module.storage.local_root / ".chunk-uploads" / upload_id / "parts"
        self.assertEqual(sorted(path.name for path in part_dir.iterdir()), ["0-0.part", "0-1.part", "0-2.part"])
        self.assertEqual((part_dir / "0-0.part").read_bytes(), payload[:4])
        self.assertEqual((part_dir / "0-1.part").read_bytes(), payload[4:8])
        self.assertEqual((part_dir / "0-2.part").read_bytes(), payload[8:])
        self.assertFalse((part_dir / "0.part").exists())
        too_wide = andy.post(
            f"/upload/{upload_id}/chunk",
            data={"csrf": csrf, "file_index": "0", "index": "0"},
            files={"chunk": ("chunk", b"abcde", "application/octet-stream")},
        )
        self.assertEqual(too_wide.status_code, 400, too_wide.text)
        self.assertEqual((part_dir / "0-0.part").read_bytes(), payload[:4])
        done = andy.post(f"/upload/{upload_id}/complete", json={"csrf": csrf})
        self.assertEqual(done.status_code, 200, done.text)
        sub_id = done.json()["id"]
        folder = app_module.storage.local_root / sub_id
        stored = (folder / "files" / "secret.txt").read_bytes()
        self.assertEqual(stored, payload)
        self.assertFalse((app_module.storage.local_root / ".chunk-uploads" / upload_id).exists())
        self.assertFalse(outside.exists())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["purpose"], "orders")
        self.assertEqual(manifest["record_date_start"], "2026-11-02")
        self.assertEqual(manifest["record_date_end"], "2026-11-02")
        self.assertEqual(manifest["note"], "分段測試")
        self.assertEqual(manifest["files"], ["secret.txt"])
        self.assertEqual(manifest["submitter"], "Andy")

    def test_login_csrf_and_expiry(self) -> None:
        anon = TestClient(app_module.app)
        refused = anon.post("/upload/start", json={"csrf": "x", "files": [{"name": "a.txt", "size": 1}]})
        self.assertEqual(refused.status_code, 401)
        andy = self._login("Andy")
        home = andy.get("/")
        bad = andy.post(
            "/upload/start",
            json={"csrf": "not-the-token", "purpose": "auto", "files": [{"name": "a.txt", "size": 1}]},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("請重新整理後再試。", bad.text)
        csrf = _csrf(home.text)
        with patch.object(app_module, "CHUNK_BYTES", 4):
            started = andy.post(
                "/upload/start",
                json={"csrf": csrf, "purpose": "auto", "record_mode": "", "files": [{"name": "a.txt", "size": 4}]},
            )
        upload_id = started.json()["id"]
        meta_path = app_module.storage.local_root / ".chunk-uploads" / upload_id / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["created_at"] = meta["created_at"] - CHUNK_TTL_SECONDS - 5
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        expired = andy.post(
            f"/upload/{upload_id}/chunk",
            data={"csrf": csrf, "file_index": "0", "index": "0"},
            files={"chunk": ("chunk", b"abcd", "application/octet-stream")},
        )
        self.assertEqual(expired.status_code, 400, expired.text)
        self.assertIn("已過期", expired.text)
        self.assertFalse(meta_path.exists())
        app_module.storage.cleanup_chunk_uploads()

    def _login(self, username: str) -> TestClient:
        client = TestClient(app_module.app)
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": username, "password": self.passwords[username], "csrf": _csrf(page.text)},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        return client


if __name__ == "__main__":
    unittest.main()

"""Optional WhatsApp record dates on upload. Uses generated passwords, not real ones."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-dates-"))
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
from storage import canonical_record_dates  # noqa: E402

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


class RecordDateTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._login_attempts.clear()
        accounts = app_module.storage._accounts_file()
        if accounts.exists():
            accounts.unlink()
        app_module.storage._password_records = None
        _clear_bootstrap()
        self.password = _password()
        os.environ["SITE_USERS"] = f"Andy:{self.password}"
        self.client = TestClient(app_module.app)
        page = self.client.get("/login")
        logged_in = self.client.post(
            "/login",
            data={"username": "Andy", "password": self.password, "csrf": _csrf(page.text)},
            follow_redirects=False,
        )
        self.assertEqual(logged_in.status_code, 303)
        self.assertNotIn(self.password, logged_in.headers.get("location", ""))

    def tearDown(self) -> None:
        _clear_bootstrap()

    def test_parse_and_storage_do_not_invent_dates(self) -> None:
        self.assertEqual(app_module.parse_record_dates("", "", "", ""), (None, None))
        self.assertEqual(app_module.parse_record_dates("", "2024-08-19", "2024-08-01", "2024-08-02"), (None, None))
        self.assertEqual(app_module.parse_record_dates("day", " 2024-08-19 ", "", ""), ("2024-08-19", "2024-08-19"))
        self.assertEqual(app_module.parse_record_dates("day", "", "2024-08-01", "2024-08-09"), (None, None))
        self.assertEqual(
            app_module.parse_record_dates("range", "", "2024-08-01", "2024-08-19"),
            ("2024-08-01", "2024-08-19"),
        )
        self.assertEqual(app_module.parse_record_dates("range", "", "2024-09-02", ""), ("2024-09-02", "2024-09-02"))
        self.assertEqual(app_module.parse_record_dates("range", "", "", "2024-09-03"), ("2024-09-03", "2024-09-03"))
        self.assertEqual(app_module.parse_record_dates("range", "", "2024-09-02", "2024-09-02"), ("2024-09-02", "2024-09-02"))
        self.assertEqual(canonical_record_dates(None, None), (None, None))
        self.assertEqual(canonical_record_dates("2024-10-01", None), ("2024-10-01", "2024-10-01"))

        with self.assertRaises(ValueError) as inverted:
            app_module.parse_record_dates("range", "", "2024-06-10", "2024-06-01")
        self.assertEqual(str(inverted.exception), "結束日期不可早於開始日期。")

        for bad in ("2024-02-31", "2024-13-01", "15/03/2024", "2024-3-15", "tomorrow"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as invalid:
                    app_module.parse_record_dates("day", bad, "", "")
                self.assertEqual(str(invalid.exception), "日期格式不正確，請選擇有效的日期。")

        with self.assertRaises(ValueError) as unknown:
            app_module.parse_record_dates("week", "2024-08-19", "", "")
        self.assertEqual(str(unknown.exception), "請選擇單日或日期範圍。")

        before = self._ids()
        with self.assertRaises(ValueError):
            app_module.storage.create_submission(
                user="Andy",
                kind="新單",
                note="",
                files=[("sample.txt", b"sample")],
                record_date_start="2024-10-10",
                record_date_end="2024-10-01",
            )
        self.assertEqual(before, self._ids())

        blank_id = app_module.storage.create_submission(
            user="Andy",
            kind="新單",
            note="",
            files=[("sample.txt", b"sample")],
            record_date_start="",
            record_date_end=None,
        )
        blank = self._manifest(blank_id)
        self.assertNotIn("record_date_start", blank)
        self.assertNotIn("record_date_end", blank)

    def test_form_offers_optional_window(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("WhatsApp 紀錄日期", home.text)
        self.assertIn("不指定", home.text)
        self.assertIn("單日", home.text)
        self.assertIn("日期範圍", home.text)
        self.assertIn("留空就會處理檔案裡找到的所有日期", home.text)
        self.assertNotRegex(home.text, r'name="record_day"[^>]*required')
        self.assertNotRegex(home.text, r'name="record_date_start"[^>]*required')
        self.assertNotRegex(home.text, r'name="record_date_end"[^>]*required')
        self.assertRegex(home.text, r'name="files"[^>]*required')
        self.assertEqual(app_module.MAX_FILE_BYTES, 40 * 1024 * 1024)
        self.assertEqual(app_module.MAX_TOTAL_BYTES, 80 * 1024 * 1024)

    def test_blank_dates_are_omitted_and_labeled(self) -> None:
        sub_id = self._submit({"record_mode": ""})
        manifest = self._manifest(sub_id)
        self.assertNotIn("record_date_start", manifest)
        self.assertNotIn("record_date_end", manifest)
        detail = self.client.get(f"/submission/{sub_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('<span class="unset">未指定日期</span>', detail.text)
        self.assertIn("未指定日期", self._card(self.client.get("/").text, sub_id))
        self.assertNotIn("至", self._card(self.client.get("/").text, sub_id))

    def test_unspecified_mode_ignores_posted_dates(self) -> None:
        sub_id = self._submit({"record_mode": "", "record_day": "2024-08-19", "record_date_start": "2024-08-01", "record_date_end": "2024-08-19"})
        manifest = self._manifest(sub_id)
        self.assertNotIn("record_date_start", manifest)
        self.assertNotIn("record_date_end", manifest)
        detail = self.client.get(f"/submission/{sub_id}")
        self.assertIn("未指定日期", detail.text)
        self.assertNotIn("2024-08-19", detail.text)

    def test_single_day_is_stored_as_same_start_and_end(self) -> None:
        sub_id = self._submit({"record_mode": "day", "record_day": "2024-08-19", "kind": "聯絡人截圖"})
        manifest = self._manifest(sub_id)
        self.assertEqual(manifest["record_date_start"], "2024-08-19")
        self.assertEqual(manifest["record_date_end"], "2024-08-19")
        self.assertEqual(manifest["kind"], "聯絡人截圖")
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(manifest["files"], ["notes.txt"])
        detail = self.client.get(f"/submission/{sub_id}")
        self.assertIn("<dt>紀錄日期</dt><dd>2024-08-19</dd>", detail.text)
        self.assertNotIn("2024-08-19 至", detail.text)
        card = self._card(self.client.get("/").text, sub_id)
        self.assertIn("紀錄 2024-08-19", card)
        self.assertNotIn("未指定日期", card)

    def test_range_is_shown_on_list_and_detail(self) -> None:
        sub_id = self._submit(
            {"record_mode": "range", "record_date_start": "2024-08-01", "record_date_end": "2024-08-19"}
        )
        manifest = self._manifest(sub_id)
        self.assertEqual(manifest["record_date_start"], "2024-08-01")
        self.assertEqual(manifest["record_date_end"], "2024-08-19")
        detail = self.client.get(f"/submission/{sub_id}")
        self.assertIn("<dt>紀錄日期</dt><dd>2024-08-01 至 2024-08-19</dd>", detail.text)
        self.assertIn("紀錄 2024-08-01 至 2024-08-19", self._card(self.client.get("/").text, sub_id))

        same_day = self._submit(
            {"record_mode": "range", "record_date_start": "2024-09-02", "record_date_end": "2024-09-02"}
        )
        same = self._manifest(same_day)
        self.assertEqual(same["record_date_start"], "2024-09-02")
        self.assertEqual(same["record_date_end"], "2024-09-02")
        detail_same = self.client.get(f"/submission/{same_day}")
        self.assertIn("<dt>紀錄日期</dt><dd>2024-09-02</dd>", detail_same.text)
        self.assertNotIn("至", detail_same.text.split("紀錄日期", 1)[1][:80])

    def test_only_start_of_range_is_that_day(self) -> None:
        sub_id = self._submit({"record_mode": "range", "record_date_start": "2024-09-02", "record_date_end": ""})
        manifest = self._manifest(sub_id)
        self.assertEqual(manifest["record_date_start"], "2024-09-02")
        self.assertEqual(manifest["record_date_end"], "2024-09-02")
        self.assertIn("<dt>紀錄日期</dt><dd>2024-09-02</dd>", self.client.get(f"/submission/{sub_id}").text)

    def test_end_before_start_is_rejected(self) -> None:
        before = self._ids()
        response = self._post(
            {"record_mode": "range", "record_date_start": "2024-06-10", "record_date_end": "2024-06-01"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("結束日期不可早於開始日期。", response.text)
        self.assertNotIn("儲存失敗", response.text)
        self.assertRegex(response.text, r'name="record_mode" value="range" checked')
        self.assertIn('name="record_date_start" value="2024-06-10"', response.text)
        self.assertIn('name="record_date_end" value="2024-06-01"', response.text)
        self.assertEqual(before, self._ids())

    def test_invalid_dates_are_rejected(self) -> None:
        before = self._ids()
        for day in ("2024-02-31", "tomorrow", "2024-3-15"):
            with self.subTest(day=day):
                response = self._post({"record_mode": "day", "record_day": day})
                self.assertEqual(response.status_code, 400)
                self.assertIn("日期格式不正確，請選擇有效的日期。", response.text)
                self.assertRegex(response.text, r'name="record_mode" value="day" checked')
        self.assertEqual(before, self._ids())

    def test_old_manifests_still_list_and_open(self) -> None:
        legacy = self._write_legacy(
            "20240115-090000-legacy",
            {
                "id": "20240115-090000-legacy",
                "submitted_at": "2024-01-15T09:00:00+08:00",
                "submitter": "Debbie",
                "kind": "新單",
                "note": "",
                "files": ["old-chat.txt"],
                "status": "done",
                "status_label": "已完成",
                "result_files": [],
                "admin_note": "",
            },
        )
        partial = self._write_legacy(
            "20240704-120000-partial",
            {
                "id": "20240704-120000-partial",
                "submitted_at": "2024-07-04T12:00:00+08:00",
                "submitter": "Taya",
                "kind": "改單",
                "note": "",
                "files": [],
                "status": "pending",
                "status_label": "待處理",
                "result_files": [],
                "admin_note": "",
                "record_date_start": "2024-07-04",
            },
        )
        garbage = self._write_legacy(
            "20240501-080000-garbage",
            {
                "id": "20240501-080000-garbage",
                "submitted_at": "2024-05-01T08:00:00+08:00",
                "submitter": "Katherine",
                "kind": "其他",
                "note": "",
                "files": [],
                "status": "pending",
                "status_label": "待處理",
                "result_files": [],
                "admin_note": "",
                "record_date_start": "tomorrow",
                "record_date_end": None,
            },
        )
        listed = {item["id"]: item for item in app_module.storage.list_submissions()}
        self.assertEqual(listed["20240115-090000-legacy"]["record_date_label"], "")
        self.assertEqual(listed["20240704-120000-partial"]["record_date_label"], "2024-07-04")
        self.assertNotIn("至", listed["20240704-120000-partial"]["record_date_label"])
        self.assertEqual(listed["20240501-080000-garbage"]["record_date_label"], "")

        home = self.client.get("/")
        self.assertIn("未指定日期", self._card(home.text, "20240115-090000-legacy"))
        self.assertIn("紀錄 2024-07-04", self._card(home.text, "20240704-120000-partial"))
        garbage_card = self._card(home.text, "20240501-080000-garbage")
        self.assertIn("未指定日期", garbage_card)
        self.assertNotIn("tomorrow", garbage_card)

        legacy_page = self.client.get("/submission/20240115-090000-legacy")
        self.assertEqual(legacy_page.status_code, 200)
        self.assertIn("已完成", legacy_page.text)
        self.assertIn('<span class="unset">未指定日期</span>', legacy_page.text)
        self.assertIn("old-chat.txt", legacy_page.text)

        partial_page = self.client.get("/submission/20240704-120000-partial")
        self.assertEqual(partial_page.status_code, 200)
        self.assertIn("<dt>紀錄日期</dt><dd>2024-07-04</dd>", partial_page.text)

        garbage_page = self.client.get("/submission/20240501-080000-garbage")
        self.assertEqual(garbage_page.status_code, 200)
        self.assertIn("未指定日期", garbage_page.text)
        self.assertNotIn("tomorrow", garbage_page.text)

        app_module.storage.get_submission("20240115-090000-legacy")
        app_module.storage.list_submissions()
        self.assertEqual(legacy.read_bytes(), self._legacy_bytes["20240115-090000-legacy"])
        self.assertEqual(partial.read_bytes(), self._legacy_bytes["20240704-120000-partial"])
        self.assertEqual(garbage.read_bytes(), self._legacy_bytes["20240501-080000-garbage"])

    def test_submit_still_requires_login_and_csrf(self) -> None:
        before = self._ids()
        anon = TestClient(app_module.app)
        blocked = anon.post(
            "/submit",
            data={"kind": "新單", "record_mode": "day", "record_day": "2024-08-19"},
            files=self._file(),
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 303)
        self.assertIn("/login", blocked.headers["location"])

        denied = self.client.post(
            "/submit",
            data={"csrf": "not-the-token", "kind": "新單", "record_mode": "day", "record_day": "2024-08-19"},
            files=self._file(),
        )
        self.assertEqual(denied.status_code, 400)
        self.assertIn("請重新整理後再提交。", denied.text)
        self.assertNotIn(self.password, denied.text)
        self.assertEqual(before, self._ids())

    def _post(self, fields: dict[str, str]):
        home = self.client.get("/")
        data = {
            "csrf": _csrf(home.text),
            "kind": fields.get("kind", "新單"),
            "note": "",
            "record_mode": fields.get("record_mode", ""),
            "record_day": fields.get("record_day", ""),
            "record_date_start": fields.get("record_date_start", ""),
            "record_date_end": fields.get("record_date_end", ""),
        }
        return self.client.post("/submit", data=data, files=self._file(), follow_redirects=False)

    def _submit(self, fields: dict[str, str]) -> str:
        response = self._post(fields)
        self.assertEqual(response.status_code, 303, response.text)
        match = re.search(r"/submission/([^?]+)", response.headers["location"])
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def _file(self):
        return [("files", ("notes.txt", b"sample export", "text/plain"))]

    def _ids(self) -> set[str]:
        root = app_module.storage.local_root
        return {path.name for path in root.iterdir() if (path / "manifest.json").is_file()}

    def _manifest(self, sub_id: str) -> dict:
        return json.loads((app_module.storage.local_root / sub_id / "manifest.json").read_text(encoding="utf-8"))

    def _card(self, html: str, sub_id: str) -> str:
        match = re.search(
            rf'<a class="feed-link" href="/submission/{re.escape(sub_id)}">.*?</a>',
            html,
            re.S,
        )
        self.assertIsNotNone(match, sub_id)
        assert match is not None
        return match.group(0)

    def _write_legacy(self, sub_id: str, manifest: dict) -> Path:
        folder = app_module.storage.local_root / sub_id
        (folder / "files").mkdir(parents=True, exist_ok=True)
        (folder / "results").mkdir(exist_ok=True)
        if manifest.get("files"):
            for name in manifest["files"]:
                (folder / "files" / name).write_bytes(b"legacy")
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if not hasattr(self, "_legacy_bytes"):
            self._legacy_bytes = {}
        self._legacy_bytes[sub_id] = path.read_bytes()
        return path


if __name__ == "__main__":
    unittest.main()

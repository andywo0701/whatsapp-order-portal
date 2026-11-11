"""Super Admin soft-delete. Uses generated passwords, not real ones."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-delete-"))
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

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
CONFIRM = "確定刪除此提交？"
ADMINS = ("Debbie", "Taya", "Katherine")


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


class _FakeHub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[dict] = []
        self.other: list[str] = []

    def upload_file(self, **kwargs):
        if self.fail:
            raise RuntimeError("hub down")
        self.uploads.append(kwargs)

    def __getattr__(self, name: str):
        def _unexpected(*_args, **_kwargs):
            self.other.append(name)
            raise AssertionError(f"unexpected hub call {name}")

        return _unexpected


class DeleteSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._login_attempts.clear()
        storage = app_module.storage
        storage.repo_id = ""
        storage.token = ""
        storage._api = None
        storage._ensured = False
        storage._password_records = None
        storage._refresh_from_hub = StorageRefresh
        accounts = storage._accounts_file()
        if accounts.exists():
            accounts.unlink()
        _clear_bootstrap()
        self.passwords = {name: _password() for name in STAFF}
        os.environ["SITE_USERS"] = "\n".join(
            f"{name}:{self.passwords[name]}" for name in STAFF
        )

    def tearDown(self) -> None:
        _clear_bootstrap()
        app_module.storage.repo_id = ""
        app_module.storage.token = ""
        app_module.storage._api = None
        app_module.storage._ensured = False

    def test_roster_limits_and_date_picker_stay(self) -> None:
        self.assertEqual(list(STAFF), ["Andy", "Debbie", "Taya", "Katherine"])
        self.assertEqual(STAFF["Andy"], "super_admin")
        self.assertEqual({STAFF[name] for name in ADMINS}, {"admin"})
        self.assertEqual(app_module.DELETE_CONFIRM, CONFIRM)
        self.assertEqual(app_module.MAX_FILE_BYTES, 40 * 1024 * 1024)
        self.assertEqual(app_module.MAX_TOTAL_BYTES, 80 * 1024 * 1024)
        self.assertEqual(app_module.MAX_FILES, 40)

        andy = self._login("Andy")
        home = andy.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("WhatsApp 紀錄日期", home.text)
        self.assertIn("不指定", home.text)
        self.assertIn("單日", home.text)
        self.assertIn("日期範圍", home.text)
        self.assertIn("留空就會處理檔案裡找到的所有日期", home.text)
        self.assertIn("單檔上限 40MB，一次最多 80MB", home.text)
        self.assertNotRegex(home.text, r'name="record_day"[^>]*required')
        self.assertNotRegex(home.text, r'name="record_date_start"[^>]*required')
        self.assertNotRegex(home.text, r'name="record_date_end"[^>]*required')
        self.assertRegex(home.text, r'name="files"[^>]*required')
        self.assertNotIn(self.passwords["Andy"], home.text)

    def test_admins_cannot_delete(self) -> None:
        sub_id = self._make(note="keep-me", payload=b"admin-blocked")
        before = self._manifest_bytes(sub_id)
        file_before = self._file_bytes(sub_id, "notes.txt")

        for name in ADMINS:
            with self.subTest(name=name):
                client = self._login(name)
                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn(f"/submission/{sub_id}", home.text)
                self.assertNotIn("/delete", home.text)
                self.assertNotIn(CONFIRM, home.text)
                self.assertNotIn("刪除這筆提交", home.text)

                detail = client.get(f"/submission/{sub_id}")
                self.assertEqual(detail.status_code, 200)
                self.assertNotIn("/delete", detail.text)
                self.assertNotIn("刪除這筆提交", detail.text)
                self.assertIn("未指定日期", detail.text)

                blocked_get = client.get(f"/submission/{sub_id}/delete")
                self.assertEqual(blocked_get.status_code, 403)
                self.assertIn("沒有權限。", blocked_get.text)
                self.assertNotIn(CONFIRM, blocked_get.text)
                self.assertNotIn('name="confirm"', blocked_get.text)

                blocked_post = client.post(
                    f"/submission/{sub_id}/delete",
                    data={"csrf": _csrf(home.text), "confirm": CONFIRM},
                    follow_redirects=False,
                )
                self.assertEqual(blocked_post.status_code, 403)
                self.assertIn("沒有權限。", blocked_post.text)
                self.assertNotIn(self.passwords[name], blocked_post.text)
                self.assertEqual(self._manifest_bytes(sub_id), before)
                self.assertEqual(self._file_bytes(sub_id, "notes.txt"), file_before)
                self.assertIn(sub_id, self._listed_ids())

        with self.assertRaises(PermissionError):
            app_module.storage.soft_delete_submission(sub_id, "Debbie")
        with self.assertRaises(PermissionError):
            app_module.storage.soft_delete_submission(sub_id, "Taya")
        with self.assertRaises(PermissionError):
            app_module.storage.soft_delete_submission(sub_id, "Katherine")
        self.assertEqual(self._manifest_bytes(sub_id), before)

        anon = TestClient(app_module.app)
        anon_post = anon.post(
            f"/submission/{sub_id}/delete",
            data={"csrf": "missing", "confirm": CONFIRM},
            follow_redirects=False,
        )
        self.assertEqual(anon_post.status_code, 303)
        self.assertIn("/login", anon_post.headers["location"])
        self.assertEqual(self._manifest_bytes(sub_id), before)
        self.assertEqual(self._file_bytes(sub_id, "notes.txt"), file_before)

    def test_super_admin_deletes_after_confirm_and_csrf(self) -> None:
        andy = self._login("Andy")
        kept = self._make(
            kind="改單",
            payload=b"keep-this",
            record_date_start="2024-08-19",
            record_date_end="2024-08-19",
        )
        kept_before = self._manifest_bytes(kept)
        target = self._submit_dated(andy, "2024-01-02", "2024-01-09")
        target_file = self._file_bytes(target, "notes.txt")
        self.assertEqual(self._manifest(target)["record_date_start"], "2024-01-02")
        self.assertEqual(self._manifest(target)["record_date_end"], "2024-01-09")
        self.assertEqual(self._manifest(target)["files"], ["notes.txt"])

        home = andy.get("/")
        self.assertIn(f'href="/submission/{target}/delete"', home.text)
        self.assertIn(f'href="/submission/{kept}/delete"', home.text)
        self.assertIn("紀錄 2024-08-19", home.text)
        self.assertIn("紀錄 2024-01-02 至 2024-01-09", home.text)
        detail = andy.get(f"/submission/{target}")
        self.assertIn(f'href="/submission/{target}/delete"', detail.text)
        self.assertIn("刪除這筆提交", detail.text)
        self.assertIn("<dt>紀錄日期</dt><dd>2024-01-02 至 2024-01-09</dd>", detail.text)

        confirm_page = andy.get(f"/submission/{target}/delete")
        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn(f"<h1>{CONFIRM}</h1>", confirm_page.text)
        self.assertIn(f'name="confirm" value="{CONFIRM}"', confirm_page.text)
        self.assertIn("2024-01-02 至 2024-01-09", confirm_page.text)
        self.assertIn("取消", confirm_page.text)

        bad_csrf = andy.post(
            f"/submission/{target}/delete",
            data={"csrf": "not-the-token", "confirm": CONFIRM},
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", bad_csrf.text)
        self.assertIn(target, self._listed_ids())
        self.assertEqual(self._manifest(target)["status"], "pending")
        self.assertNotIn("deleted_by", self._manifest(target))

        for wrong in ("", "yes", "確定刪除", "確定刪除此提交"):
            with self.subTest(confirm=wrong):
                rejected = andy.post(
                    f"/submission/{target}/delete",
                    data={"csrf": _csrf(confirm_page.text), "confirm": wrong},
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("請先確認再刪除。", rejected.text)
                self.assertIn(f"<h1>{CONFIRM}</h1>", rejected.text)
                self.assertEqual(self._manifest(target)["status"], "pending")
                self.assertEqual(self._file_bytes(target, "notes.txt"), target_file)

        original_delete = app_module.storage.soft_delete_submission

        def _boom(_sub_id: str, _actor: str):
            raise RuntimeError("hub down")

        app_module.storage.soft_delete_submission = _boom
        try:
            failed = andy.post(
                f"/submission/{target}/delete",
                data={"csrf": _csrf(confirm_page.text), "confirm": CONFIRM},
            )
        finally:
            app_module.storage.soft_delete_submission = original_delete
        self.assertEqual(failed.status_code, 500)
        self.assertIn("暫時無法刪除，請稍後再試。", failed.text)
        self.assertEqual(self._manifest(target)["status"], "pending")
        self.assertIn(target, self._listed_ids())
        self.assertEqual(self._file_bytes(target, "notes.txt"), target_file)

        deleted = andy.post(
            f"/submission/{target}/delete",
            data={"csrf": _csrf(andy.get(f"/submission/{target}/delete").text), "confirm": CONFIRM},
            follow_redirects=False,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/?notice=deleted")
        self.assertNotIn(self.passwords["Andy"], deleted.headers["location"])

        home_after = andy.get(deleted.headers["location"])
        self.assertIn("已刪除此提交，近期列表不會再顯示。", home_after.text)
        self.assertNotIn(f"/submission/{target}", home_after.text)
        self.assertIn(f"/submission/{kept}", home_after.text)
        self.assertIn("紀錄 2024-08-19", home_after.text)
        self.assertNotIn("2024-01-02 至 2024-01-09", home_after.text)
        self.assertNotIn(target, self._listed_ids())
        self.assertIn(kept, self._listed_ids())

        manifest = self._manifest(target)
        self.assertEqual(manifest["status"], "deleted")
        self.assertEqual(manifest["status_label"], "已刪除")
        self.assertEqual(manifest["deleted_by"], "Andy")
        self.assertTrue(manifest["deleted_at"])
        self.assertIn("+08:00", manifest["deleted_at"])
        self.assertEqual(manifest["record_date_start"], "2024-01-02")
        self.assertEqual(manifest["record_date_end"], "2024-01-09")
        self.assertEqual(manifest["files"], ["notes.txt"])
        self.assertNotIn(self.passwords["Andy"], json.dumps(manifest))
        self.assertEqual(self._file_bytes(target, "notes.txt"), target_file)
        self.assertEqual(self._manifest_bytes(kept), kept_before)

        opened = andy.get(f"/submission/{target}")
        self.assertEqual(opened.status_code, 200)
        self.assertIn("已刪除", opened.text)
        self.assertIn("由 Andy", opened.text)
        self.assertIn("HKT", opened.text)
        self.assertIn("2024-01-02 至 2024-01-09", opened.text)
        self.assertNotIn("刪除這筆提交", opened.text)
        downloaded = andy.get(f"/download/{target}/files/notes.txt")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, target_file)

        again_page = andy.get(f"/submission/{target}/delete")
        self.assertEqual(again_page.status_code, 200)
        self.assertIn("這筆提交已經刪除", again_page.text)
        self.assertNotIn('name="confirm"', again_page.text)
        stamped = manifest["deleted_at"]
        again = andy.post(
            f"/submission/{target}/delete",
            data={"csrf": _csrf(andy.get("/").text), "confirm": CONFIRM},
            follow_redirects=False,
        )
        self.assertEqual(again.status_code, 303)
        self.assertEqual(self._manifest(target)["deleted_at"], stamped)
        self.assertEqual(self._manifest(target)["deleted_by"], "Andy")
        self.assertEqual(self._file_bytes(target, "notes.txt"), target_file)

        debbie = self._login("Debbie")
        debbie_home = debbie.get("/")
        self.assertNotIn(f"/submission/{target}", debbie_home.text)
        self.assertNotIn("/delete", debbie_home.text)
        debbie_detail = debbie.get(f"/submission/{target}")
        self.assertEqual(debbie_detail.status_code, 200)
        self.assertIn("已刪除", debbie_detail.text)
        self.assertNotIn("刪除這筆提交", debbie_detail.text)

    def test_legacy_manifests_still_load(self) -> None:
        legacy = self._write_legacy(
            "20181201-100000-legacy",
            {
                "id": "20181201-100000-legacy",
                "submitted_at": "2018-12-01T10:00:00+08:00",
                "submitter": "Debbie",
                "kind": "新單",
                "note": "舊備註",
                "files": ["old-chat.txt"],
                "status": "done",
                "result_files": ["order.xlsx"],
                "admin_note": "",
            },
            files={"old-chat.txt": b"legacy-chat"},
            results={"order.xlsx": b"legacy-excel"},
        )
        partial = self._write_legacy(
            "20190604-120000-partial",
            {
                "id": "20190604-120000-partial",
                "submitted_at": "2019-06-04T12:00:00+08:00",
                "submitter": "Taya",
                "kind": "改單",
                "note": "",
                "files": [],
                "status": "pending",
                "status_label": "待處理",
                "result_files": [],
                "admin_note": "",
                "record_date_start": "2019-06-04",
            },
        )
        garbage = self._write_legacy(
            "20190501-080000-nodate",
            {
                "id": "20190501-080000-nodate",
                "submitted_at": "2019-05-01T08:00:00+08:00",
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
        legacy_before = legacy.read_bytes()
        partial_before = partial.read_bytes()
        garbage_before = garbage.read_bytes()

        andy = self._login("Andy")
        listed = {item["id"]: item for item in app_module.storage.list_submissions()}
        self.assertIn("20181201-100000-legacy", listed)
        self.assertFalse(listed["20181201-100000-legacy"]["deleted"])
        self.assertEqual(listed["20181201-100000-legacy"]["status_label"], "已完成")
        self.assertEqual(listed["20181201-100000-legacy"]["record_date_label"], "")
        self.assertEqual(listed["20190604-120000-partial"]["record_date_label"], "2019-06-04")
        self.assertFalse(listed["20190604-120000-partial"]["deleted"])
        self.assertEqual(listed["20190501-080000-nodate"]["record_date_label"], "")
        self.assertFalse(listed["20190501-080000-nodate"]["deleted"])

        page = andy.get("/submission/20181201-100000-legacy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("已完成", page.text)
        self.assertIn("舊備註", page.text)
        self.assertIn("old-chat.txt", page.text)
        self.assertIn("order.xlsx", page.text)
        self.assertIn('<span class="unset">未指定日期</span>', page.text)
        self.assertNotIn("已刪除", page.text)
        partial_page = andy.get("/submission/20190604-120000-partial")
        self.assertEqual(partial_page.status_code, 200)
        self.assertIn("<dt>紀錄日期</dt><dd>2019-06-04</dd>", partial_page.text)
        garbage_page = andy.get("/submission/20190501-080000-nodate")
        self.assertEqual(garbage_page.status_code, 200)
        self.assertIn("未指定日期", garbage_page.text)
        self.assertNotIn("tomorrow", garbage_page.text)

        app_module.storage.get_submission("20181201-100000-legacy")
        app_module.storage.list_submissions()
        self.assertEqual(legacy.read_bytes(), legacy_before)
        self.assertEqual(partial.read_bytes(), partial_before)
        self.assertEqual(garbage.read_bytes(), garbage_before)
        self.assertNotIn(b"deleted_by", legacy_before)
        self.assertNotIn(b"deleted_at", legacy_before)

        removed = andy.post(
            "/submission/20181201-100000-legacy/delete",
            data={
                "csrf": _csrf(andy.get("/submission/20181201-100000-legacy/delete").text),
                "confirm": CONFIRM,
            },
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        self.assertNotIn("20181201-100000-legacy", self._listed_ids())
        self.assertIn("20190604-120000-partial", self._listed_ids())
        self.assertIn("20190501-080000-nodate", self._listed_ids())
        saved = self._manifest("20181201-100000-legacy")
        self.assertEqual(saved["status"], "deleted")
        self.assertEqual(saved["status_label"], "已刪除")
        self.assertEqual(saved["deleted_by"], "Andy")
        self.assertEqual(saved["files"], ["old-chat.txt"])
        self.assertEqual(saved["result_files"], ["order.xlsx"])
        self.assertEqual(saved["note"], "舊備註")
        self.assertNotIn("record_date_start", saved)
        self.assertEqual(self._file_bytes("20181201-100000-legacy", "old-chat.txt"), b"legacy-chat")
        self.assertEqual(
            (app_module.storage.local_root / "20181201-100000-legacy" / "results" / "order.xlsx").read_bytes(),
            b"legacy-excel",
        )
        reopened = andy.get("/submission/20181201-100000-legacy")
        self.assertEqual(reopened.status_code, 200)
        self.assertIn("已刪除", reopened.text)
        self.assertIn("old-chat.txt", reopened.text)
        self.assertEqual(partial.read_bytes(), partial_before)
        self.assertEqual(garbage.read_bytes(), garbage_before)

    def test_hub_updates_manifest_only(self) -> None:
        sub_id = self._make(payload=b"stay-on-disk", note="hub")
        folder = app_module.storage.local_root / sub_id
        original_file = (folder / "files" / "notes.txt").read_bytes()
        original_manifest = (folder / "manifest.json").read_bytes()
        names_before = self._filenames(sub_id)

        storage = app_module.storage
        failing = _FakeHub(fail=True)
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._api = failing
        storage._ensured = True
        storage._refresh_from_hub = lambda _sub_id: None
        try:
            with self.assertRaises(RuntimeError):
                storage.soft_delete_submission(sub_id, "Andy")
            self.assertEqual((folder / "manifest.json").read_bytes(), original_manifest)
            self.assertEqual((folder / "files" / "notes.txt").read_bytes(), original_file)
            self.assertEqual(failing.uploads, [])
            self.assertEqual(failing.other, [])

            fake = _FakeHub()
            storage._api = fake
            updated = storage.soft_delete_submission(sub_id, "Andy")
            self.assertTrue(updated["deleted"])
            self.assertEqual(updated["deleted_by"], "Andy")
            self.assertEqual(len(fake.uploads), 1)
            upload = fake.uploads[0]
            self.assertEqual(upload["path_in_repo"], f"inbox/{sub_id}/manifest.json")
            self.assertEqual(upload["repo_id"], "example/inbox")
            self.assertEqual(upload["repo_type"], "dataset")
            self.assertEqual(upload["commit_message"], f"Mark submission {sub_id} deleted")
            self.assertIsInstance(upload["path_or_fileobj"], bytes)
            body = json.loads(upload["path_or_fileobj"].decode("utf-8"))
            self.assertEqual(body["status"], "deleted")
            self.assertEqual(body["status_label"], "已刪除")
            self.assertEqual(body["files"], ["notes.txt"])
            self.assertEqual(body["note"], "hub")
            self.assertNotIn("notes.txt", upload["commit_message"])
            self.assertEqual((folder / "manifest.json").read_bytes(), upload["path_or_fileobj"])
            self.assertEqual((folder / "files" / "notes.txt").read_bytes(), original_file)
            self.assertEqual(self._filenames(sub_id), names_before)
            self.assertEqual(fake.other, [])

            storage.soft_delete_submission(sub_id, "Andy")
            self.assertEqual(len(fake.uploads), 1)
        finally:
            storage.repo_id = ""
            storage.token = ""
            storage._api = None
            storage._ensured = False
            storage._refresh_from_hub = StorageRefresh

    def _login(self, username: str) -> TestClient:
        client = TestClient(app_module.app)
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "username": username,
                "password": self.passwords[username],
                "csrf": _csrf(page.text),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertNotIn(self.passwords[username], response.headers.get("location", ""))
        return client

    def _make(self, **kwargs) -> str:
        payload = kwargs.pop("payload", b"sample export")
        note = kwargs.pop("note", "")
        kind = kwargs.pop("kind", "新單")
        return app_module.storage.create_submission(
            user="Debbie",
            kind=kind,
            note=note,
            files=[("notes.txt", payload)],
            record_date_start=kwargs.get("record_date_start"),
            record_date_end=kwargs.get("record_date_end"),
        )

    def _submit_dated(self, client: TestClient, start: str, end: str) -> str:
        home = client.get("/")
        response = client.post(
            "/submit",
            data={
                "csrf": _csrf(home.text),
                "kind": "新單",
                "note": "",
                "record_mode": "range",
                "record_day": "",
                "record_date_start": start,
                "record_date_end": end,
            },
            files=[("files", ("notes.txt", b"dated export", "text/plain"))],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        match = re.search(r"/submission/([^?]+)", response.headers["location"])
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def _manifest(self, sub_id: str) -> dict:
        return json.loads(self._manifest_bytes(sub_id).decode("utf-8"))

    def _manifest_bytes(self, sub_id: str) -> bytes:
        return (app_module.storage.local_root / sub_id / "manifest.json").read_bytes()

    def _file_bytes(self, sub_id: str, name: str) -> bytes:
        return (app_module.storage.local_root / sub_id / "files" / name).read_bytes()

    def _filenames(self, sub_id: str) -> list[str]:
        root = app_module.storage.local_root / sub_id
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def _listed_ids(self) -> set[str]:
        return {item["id"] for item in app_module.storage.list_submissions()}

    def _write_legacy(
        self,
        sub_id: str,
        manifest: dict,
        files: dict[str, bytes] | None = None,
        results: dict[str, bytes] | None = None,
    ) -> Path:
        folder = app_module.storage.local_root / sub_id
        (folder / "files").mkdir(parents=True, exist_ok=True)
        (folder / "results").mkdir(exist_ok=True)
        for name, data in (files or {}).items():
            (folder / "files" / name).write_bytes(data)
        for name, data in (results or {}).items():
            (folder / "results" / name).write_bytes(data)
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


StorageRefresh = app_module.storage._refresh_from_hub


if __name__ == "__main__":
    unittest.main()

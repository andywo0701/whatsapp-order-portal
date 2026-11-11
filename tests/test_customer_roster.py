"""Customer-data uploads and the staff roster. Uses generated passwords and fake numbers."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-roster-"))
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
from huggingface_hub.errors import EntryNotFoundError  # noqa: E402
from roster import (  # noqa: E402
    empty_document,
    merge_rosters,
    normalize_document,
    phone_key,
)

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
ADMINS = ("Debbie", "Taya", "Katherine")
CONFIRM = "確定確認此顧客？"
EDIT_CONFIRM = "確定儲存此更正？"
DELETE_CONFIRM = "確定刪除此顧客？"
SUBMISSION_DELETE = "確定刪除此提交？"


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
        self.commits: list[dict] = []

    def create_commit(self, **kwargs):
        if self.fail:
            raise RuntimeError("hub down")
        self.commits.append(kwargs)

    def upload_file(self, **_kwargs):
        raise AssertionError("roster publish should use one dataset commit")


class CustomerRosterTests(unittest.TestCase):
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
        folder = storage.local_root / "customers"
        if folder.exists():
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink()
            folder.rmdir()
        _clear_bootstrap()
        self.passwords = {name: _password() for name in STAFF}
        os.environ["SITE_USERS"] = "\n".join(f"{name}:{self.passwords[name]}" for name in STAFF)
        self.seq = 100

    def tearDown(self) -> None:
        _clear_bootstrap()
        app_module.storage.repo_id = ""
        app_module.storage.token = ""
        app_module.storage._api = None
        app_module.storage._ensured = False

    def test_merge_rules_do_not_invent_or_pick(self) -> None:
        kept_phone = self._phone()
        other_phone = self._phone()
        name = self._name()
        local = {
            "version": 1,
            "customers": [
                {
                    "id": "c0000aa01",
                    "name": name,
                    "phone": kept_phone,
                    "phone_source": "conversation",
                    "status": "ok",
                }
            ],
        }
        blanked = merge_rosters(
            local,
            {"version": 1, "updated_by": "scanner", "customers": [{"name": name, "phone": ""}]},
        )
        self.assertEqual(blanked["customers"][0]["phone"], kept_phone)
        self.assertEqual(phone_key(blanked["customers"][0]["phone"]), phone_key(kept_phone))
        self.assertNotEqual(blanked["customers"][0]["phone"], "")

        conflict = merge_rosters(
            local,
            {
                "version": 1,
                "updated_by": "scanner",
                "customers": [
                    {"name": name, "phone": other_phone, "phone_source": "contact_screenshot"}
                ],
            },
        )
        row = conflict["customers"][0]
        stored = {item["phone"] for item in row["phones"]}
        self.assertEqual(stored, {kept_phone, other_phone})
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["phone"], kept_phone)
        self.assertNotEqual(row["phone"], other_phone)
        self.assertEqual(len(conflict["customers"]), 1)

        invented = merge_rosters(
            empty_document(),
            {
                "customers": [
                    {"name": "", "phone": ""},
                    {"name": None, "phone": 101},
                    {"顧客姓名": self._name(), "電話": 202},
                ]
            },
        )
        self.assertEqual(len(invented["customers"]), 1)
        self.assertEqual(invented["customers"][0]["phone"], "")
        self.assertEqual(invented["customers"][0]["phones"], [])

        self.assertEqual(merge_rosters(local, {})["customers"][0]["phone"], kept_phone)

    def test_empty_roster_and_missing_customers_folder(self) -> None:
        self.assertFalse((app_module.storage.local_root / "customers").exists())
        andy = self._login("Andy")
        page = andy.get("/customers")
        self.assertEqual(page.status_code, 200)
        self.assertIn("還沒有顧客名冊", page.text)
        self.assertIn("客戶資料", page.text)
        self.assertIn("尚缺電話", page.text)
        self.assertIn("待確認", page.text)
        self.assertIn('href="/customers/roster.xlsx"', page.text)
        self.assertNotIn(self.passwords["Andy"], page.text)
        workbook = andy.get("/customers/roster.xlsx")
        self.assertEqual(workbook.status_code, 200)
        self.assertIn("roster.xlsx", workbook.headers["content-disposition"])
        self.assertIn("filename*=UTF-8''", workbook.headers["content-disposition"])
        sheet = self._sheet(workbook.content)
        self.assertIn("顧客姓名", sheet)
        self.assertIn("電話", sheet)
        self.assertIn("號碼來源", sheet)
        self.assertIn("最後更新日期", sheet)
        self.assertIn("狀態", sheet)
        self.assertIn("備註／待確認", sheet)
        self.assertFalse((app_module.storage.local_root / "customers").exists())

        health = andy.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"ok": "true"})

        anon = TestClient(app_module.app)
        for path in ("/customers", "/customers/roster.xlsx", "/customers/c0000aa01", "/customers/c0000aa01/edit"):
            blocked = anon.get(path, follow_redirects=False)
            self.assertEqual(blocked.status_code, 303, path)
            self.assertIn("/login", blocked.headers["location"])

        storage = app_module.storage
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._ensured = True
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("customers/roster.json is absent")):
            again = andy.get("/customers")
        self.assertEqual(again.status_code, 200)
        self.assertIn("還沒有顧客名冊", again.text)
        self.assertFalse((storage.local_root / "customers").exists())

    def test_purpose_is_stored_and_old_manifests_still_open(self) -> None:
        home = self._login("Andy").get("/")
        self.assertIn("落單資料", home.text)
        self.assertIn("客戶資料", home.text)
        self.assertIn("WhatsApp 紀錄日期", home.text)
        self.assertIn("不指定", home.text)
        self.assertIn("單日", home.text)
        self.assertIn("日期範圍", home.text)
        self.assertRegex(home.text, r'name="purpose" value="orders" checked')
        self.assertIn('href="/customers"', home.text)
        chosen = self._login("Andy")
        preselected = chosen.get("/?purpose=customers")
        self.assertRegex(preselected.text, r'name="purpose" value="customers" checked')

        debbie = self._login("Debbie")
        customer_id = self._submit(
            debbie,
            {"purpose": "customers", "record_mode": "day", "record_day": "2024-08-19", "kind": "聯絡人截圖"},
            b"fake-contact-export",
        )
        manifest = self._manifest(customer_id)
        self.assertEqual(manifest["purpose"], "customers")
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(manifest["status_label"], "待處理")
        self.assertEqual(manifest["record_date_start"], "2024-08-19")
        self.assertEqual(manifest["record_date_end"], "2024-08-19")
        self.assertEqual(manifest["files"], ["notes.txt"])
        listed = debbie.get("/")
        card = self._card(listed.text, customer_id)
        self.assertIn("客戶資料", card)
        detail = debbie.get(f"/submission/{customer_id}")
        self.assertIn("客戶資料", detail.text)
        self.assertIn("不會從圖片讀取姓名或電話", detail.text)
        self.assertEqual(detail.status_code, 200)

        orders = self._submit(debbie, {}, b"fake-order-export")
        self.assertEqual(self._manifest(orders)["purpose"], "orders")
        self.assertIn("落單資料", self._card(debbie.get("/").text, orders))

        legacy_path = self._write_legacy(
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
        before = legacy_path.read_bytes()
        self.assertNotIn(b"purpose", before)
        opened = debbie.get("/submission/20240115-090000-legacy")
        self.assertEqual(opened.status_code, 200)
        self.assertIn("落單資料", opened.text)
        self.assertIn("已完成", opened.text)
        self.assertIn("old-chat.txt", opened.text)
        app_module.storage.list_submissions()
        self.assertEqual(legacy_path.read_bytes(), before)
        presented = app_module.storage.get_submission("20240115-090000-legacy")
        self.assertEqual(presented["purpose"], "orders")
        self.assertEqual(presented["purpose_label"], "落單資料")

        andy = self._login("Andy")
        removed = andy.post(
            f"/submission/{customer_id}/delete",
            data={"csrf": _csrf(andy.get(f"/submission/{customer_id}/delete").text), "confirm": SUBMISSION_DELETE},
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        saved = self._manifest(customer_id)
        self.assertEqual(saved["status"], "deleted")
        self.assertEqual(saved["purpose"], "customers")
        self.assertEqual(saved["deleted_by"], "Andy")
        self.assertEqual((app_module.storage.local_root / customer_id / "files" / "notes.txt").read_bytes(), b"fake-contact-export")
        self.assertNotIn(customer_id, {item["id"] for item in app_module.storage.list_submissions()})
        blocked = debbie.get(f"/submission/{customer_id}/delete")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("沒有權限。", blocked.text)

    def test_staff_can_view_and_download_but_not_mutate(self) -> None:
        name = self._name()
        phone = self._phone()
        missing_name = self._name()
        doc = self._write_roster(
            [
                {"id": "c0000bb01", "name": name, "phone": phone, "phone_source": "conversation", "status": "pending"},
                {"id": "c0000bb02", "name": missing_name, "phone": "", "status": "ok"},
            ]
        )
        before = self._roster_bytes()
        submission = self._submit(self._login("Katherine"), {"purpose": "customers"}, b"linked-export")
        linked = doc["customers"][0]
        linked["submission_ids"] = [submission]
        self._write_roster(doc["customers"])
        before = self._roster_bytes()

        for username in ADMINS:
            with self.subTest(username=username):
                client = self._login(username)
                page = client.get("/customers")
                self.assertEqual(page.status_code, 200)
                self.assertIn(name, page.text)
                self.assertIn(phone, page.text)
                self.assertIn("尚缺", page.text)
                self.assertNotIn(f"/customers/c0000bb01/edit", page.text)
                found = client.get(f"/customers?q={phone.replace('-', '')}")
                self.assertIn(name, found.text)
                self.assertNotIn(missing_name, found.text)
                by_name = client.get("/customers", params={"q": missing_name, "filter": "missing_phone"})
                self.assertIn(missing_name, by_name.text)
                self.assertNotIn(phone, by_name.text)
                pending = client.get("/customers", params={"filter": "pending"})
                self.assertIn(name, pending.text)
                self.assertNotIn(missing_name, pending.text)
                detail = client.get("/customers/c0000bb01")
                self.assertEqual(detail.status_code, 200)
                self.assertIn(phone, detail.text)
                self.assertIn(submission, detail.text)
                self.assertNotIn("/confirm", detail.text)
                self.assertNotIn("/edit", detail.text)
                self.assertNotIn("/delete", detail.text)
                downloaded = client.get("/customers/roster.xlsx")
                self.assertEqual(downloaded.status_code, 200)
                sheet = self._sheet(downloaded.content)
                self.assertIn(name, sheet)
                self.assertIn(phone, sheet)
                self.assertIn("尚缺", sheet)
                self.assertNotIn(self.passwords[username].encode("utf-8"), downloaded.content)
                for path in (
                    "/customers/c0000bb01/confirm",
                    "/customers/c0000bb01/edit",
                    "/customers/c0000bb01/delete",
                ):
                    blocked_get = client.get(path)
                    self.assertEqual(blocked_get.status_code, 403, path)
                    self.assertIn("沒有權限。", blocked_get.text)
                    self.assertNotIn(CONFIRM, blocked_get.text)
                    self.assertNotIn(EDIT_CONFIRM, blocked_get.text)
                    self.assertNotIn(DELETE_CONFIRM, blocked_get.text)
                    blocked_post = client.post(
                        path,
                        data={
                            "csrf": _csrf(page.text),
                            "confirm": CONFIRM,
                            "phone_choice": phone,
                            "name": name,
                            "phone": self._phone(),
                            "note": "不可以改",
                        },
                    )
                    self.assertEqual(blocked_post.status_code, 403, path)
                    self.assertIn("沒有權限。", blocked_post.text)
                self.assertEqual(self._roster_bytes(), before)

        with self.assertRaises(PermissionError):
            app_module.storage.confirm_customer("Debbie", "c0000bb01", phone)
        with self.assertRaises(PermissionError):
            app_module.storage.edit_customer("Taya", "c0000bb01", name, phone, "不可以")
        with self.assertRaises(PermissionError):
            app_module.storage.delete_customer("Katherine", "c0000bb01")
        self.assertEqual(self._roster_bytes(), before)

    def test_super_admin_confirm_and_edit_require_phrase_and_csrf(self) -> None:
        name = self._name()
        phone = self._phone()
        other = self._phone()
        self._write_roster(
            [
                {
                    "id": "c0000cc01",
                    "name": name,
                    "phone": phone,
                    "phones": [
                        {"phone": phone, "source": "conversation"},
                        {"phone": other, "source": "contact_screenshot"},
                    ],
                    "status": "pending",
                }
            ]
        )
        before = self._roster_bytes()
        andy = self._login("Andy")
        detail = andy.get("/customers/c0000cc01")
        self.assertIn(phone, detail.text)
        self.assertIn(other, detail.text)
        self.assertIn("待確認", detail.text)
        self.assertIn("沒有自動挑選", detail.text)
        confirm_page = andy.get("/customers/c0000cc01/confirm")
        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn(CONFIRM, confirm_page.text)
        self.assertIn(f'value="{phone}"', confirm_page.text)
        self.assertIn(f'value="{other}"', confirm_page.text)

        bad_csrf = andy.post(
            "/customers/c0000cc01/confirm",
            data={"csrf": "not-the-token", "confirm": CONFIRM, "phone_choice": phone},
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", bad_csrf.text)
        self.assertEqual(self._roster_bytes(), before)

        bad_phrase = andy.post(
            "/customers/c0000cc01/confirm",
            data={"csrf": _csrf(confirm_page.text), "confirm": "確定", "phone_choice": phone},
        )
        self.assertEqual(bad_phrase.status_code, 400)
        self.assertIn("請先確認再標記。", bad_phrase.text)
        self.assertEqual(self._roster_bytes(), before)

        no_choice = andy.post(
            "/customers/c0000cc01/confirm",
            data={"csrf": _csrf(confirm_page.text), "confirm": CONFIRM, "phone_choice": ""},
        )
        self.assertEqual(no_choice.status_code, 400)
        self.assertIn("不會自動挑選", no_choice.text)
        self.assertEqual(self._roster_bytes(), before)

        chosen = andy.post(
            "/customers/c0000cc01/confirm",
            data={"csrf": _csrf(andy.get("/customers/c0000cc01/confirm").text), "confirm": CONFIRM, "phone_choice": other},
            follow_redirects=False,
        )
        self.assertEqual(chosen.status_code, 303)
        saved = self._roster()
        row = saved["customers"][0]
        self.assertEqual(row["phone"], other)
        self.assertEqual(row["status"], "ok")
        self.assertEqual([item["phone"] for item in row["phones"]], [other])
        self.assertEqual(row["confirmed_by"], "Andy")
        self.assertIn("+08:00", row["confirmed_at"])
        self.assertEqual(row["history"][-1]["by"], "Andy")
        self.assertEqual(row["history"][-1]["action"], "confirm")
        self.assertNotIn(phone, [item["phone"] for item in row["phones"]])
        self.assertNotIn(self.passwords["Andy"], json.dumps(saved))

        edit_page = andy.get("/customers/c0000cc01/edit")
        self.assertIn(EDIT_CONFIRM, edit_page.text)
        self.assertIn("人手輸入", edit_page.text)
        missing_csrf = andy.post(
            "/customers/c0000cc01/edit",
            data={"csrf": "nope", "confirm": EDIT_CONFIRM, "name": name, "phone": other, "note": "更正說明"},
        )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", missing_csrf.text)
        missing_phrase = andy.post(
            "/customers/c0000cc01/edit",
            data={"csrf": _csrf(edit_page.text), "confirm": "儲存", "name": name, "phone": other, "note": "更正說明"},
        )
        self.assertEqual(missing_phrase.status_code, 400)
        self.assertIn("請先確認再儲存。", missing_phrase.text)
        missing_note = andy.post(
            "/customers/c0000cc01/edit",
            data={"csrf": _csrf(edit_page.text), "confirm": EDIT_CONFIRM, "name": name, "phone": self._phone(), "note": "  "},
        )
        self.assertEqual(missing_note.status_code, 400)
        self.assertIn("請填寫更正說明。", missing_note.text)
        self.assertEqual(self._roster()["customers"][0]["phone"], other)

        blanked = andy.post(
            "/customers/c0000cc01/edit",
            data={"csrf": _csrf(edit_page.text), "confirm": EDIT_CONFIRM, "name": name, "phone": "", "note": "想把電話清掉"},
        )
        self.assertEqual(blanked.status_code, 400)
        self.assertIn("已有電話不可改成空白。", blanked.text)
        self.assertEqual(self._roster()["customers"][0]["phone"], other)

        corrected = self._phone()
        renamed = self._name()
        updated = andy.post(
            "/customers/c0000cc01/edit",
            data={
                "csrf": _csrf(andy.get("/customers/c0000cc01/edit").text),
                "confirm": EDIT_CONFIRM,
                "name": renamed,
                "phone": corrected,
                "note": "人手核對假資料",
            },
            follow_redirects=False,
        )
        self.assertEqual(updated.status_code, 303)
        edited = self._roster()["customers"][0]
        self.assertEqual(edited["name"], renamed)
        self.assertEqual(edited["phone"], corrected)
        self.assertEqual(edited["phone_source"], "manual")
        self.assertEqual(edited["note"], "人手核對假資料")
        self.assertEqual(edited["history"][-1]["action"], "edit")
        self.assertEqual(edited["history"][-1]["by"], "Andy")
        self.assertEqual(edited["history"][-1]["before"]["phone"], other)
        self.assertEqual(edited["history"][-1]["after"]["phone"], corrected)
        shown = andy.get("/customers/c0000cc01")
        self.assertIn("人手輸入", shown.text)
        self.assertIn("由 Andy", shown.text)
        self.assertIn(corrected, shown.text)

        delete_page = andy.get("/customers/c0000cc01/delete")
        self.assertIn(DELETE_CONFIRM, delete_page.text)
        rejected = andy.post(
            "/customers/c0000cc01/delete",
            data={"csrf": _csrf(delete_page.text), "confirm": "刪除"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("請先確認再刪除。", rejected.text)
        self.assertEqual(self._roster()["customers"][0]["id"], "c0000cc01")
        deleted = andy.post(
            "/customers/c0000cc01/delete",
            data={"csrf": _csrf(delete_page.text), "confirm": DELETE_CONFIRM},
            follow_redirects=False,
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/customers?notice=deleted")
        after = andy.get(deleted.headers["location"])
        self.assertIn("已刪除此顧客。", after.text)
        self.assertNotIn(renamed, after.text)
        stored = self._roster()
        self.assertEqual(stored["customers"], [])
        self.assertEqual(stored["deleted"][0]["deleted_by"], "Andy")
        self.assertEqual(stored["deleted"][0]["phone"], corrected)
        self.assertIn("+08:00", stored["deleted"][0]["deleted_at"])

    def test_hub_failure_keeps_the_local_roster(self) -> None:
        phone = self._phone()
        name = self._name()
        self._write_roster(
            [{"id": "c0000dd01", "name": name, "phone": phone, "phone_source": "conversation", "status": "ok"}]
        )
        before = self._roster_bytes()
        workbook_before = (app_module.storage.local_root / "customers" / "roster.xlsx").read_bytes()
        andy = self._login("Andy")
        storage = app_module.storage
        failing = _FakeHub(fail=True)
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._api = failing
        storage._ensured = True

        remote = {
            "version": 1,
            "updated_by": "scanner",
            "customers": [{"id": "c0000dd01", "name": name, "phone": ""}],
        }
        remote_path = Path(tempfile.mkstemp(suffix=".json")[1])
        remote_path.write_text(json.dumps(remote), encoding="utf-8")

        def _download(**_kwargs):
            return str(remote_path)

        with patch("storage.hf_hub_download", side_effect=_download):
            page = andy.get("/customers/c0000dd01")
            self.assertIn(phone, page.text)
            self.assertNotIn(">尚缺<", page.text)
            self.assertEqual(self._roster_bytes(), before)
            failed = andy.post(
                "/customers/c0000dd01/edit",
                data={
                    "csrf": _csrf(andy.get("/customers/c0000dd01/edit").text),
                    "confirm": EDIT_CONFIRM,
                    "name": name,
                    "phone": self._phone(),
                    "note": "這次不應寫入",
                },
            )
        self.assertEqual(failed.status_code, 500)
        self.assertIn("暫時無法儲存，請稍後再試。", failed.text)
        self.assertEqual(self._roster_bytes(), before)
        self.assertEqual((storage.local_root / "customers" / "roster.xlsx").read_bytes(), workbook_before)
        self.assertEqual(failing.commits, [])

        working = _FakeHub()
        storage._api = working
        changed = self._phone()
        with patch("storage.hf_hub_download", side_effect=_download):
            ok = andy.post(
                "/customers/c0000dd01/edit",
                data={
                    "csrf": _csrf(andy.get("/customers/c0000dd01/edit").text),
                    "confirm": EDIT_CONFIRM,
                    "name": name,
                    "phone": changed,
                    "note": "發布成功後才保存",
                },
                follow_redirects=False,
            )
        self.assertEqual(ok.status_code, 303, ok.text)
        self.assertEqual(len(working.commits), 1)
        commit = working.commits[0]
        paths = {item.path_in_repo: item.path_or_fileobj for item in commit["operations"]}
        self.assertEqual(set(paths), {"customers/roster.json", "customers/roster.xlsx"})
        self.assertEqual(commit["repo_type"], "dataset")
        published = json.loads(paths["customers/roster.json"].decode("utf-8"))
        self.assertEqual(published["customers"][0]["phone"], changed)
        self.assertEqual(json.loads(self._roster_bytes().decode("utf-8"))["customers"][0]["phone"], changed)
        self.assertIn(changed, self._sheet(paths["customers/roster.xlsx"]))
        remote_path.unlink(missing_ok=True)

    def _login(self, username: str) -> TestClient:
        client = TestClient(app_module.app)
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": username, "password": self.passwords[username], "csrf": _csrf(page.text)},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertNotIn(self.passwords[username], response.headers.get("location", ""))
        return client

    def _submit(self, client: TestClient, fields: dict[str, str], payload: bytes) -> str:
        home = client.get("/")
        data = {
            "csrf": _csrf(home.text),
            "kind": fields.get("kind", "其他"),
            "note": "",
            "purpose": fields.get("purpose", ""),
            "record_mode": fields.get("record_mode", ""),
            "record_day": fields.get("record_day", ""),
            "record_date_start": fields.get("record_date_start", ""),
            "record_date_end": fields.get("record_date_end", ""),
        }
        if "purpose" not in fields:
            data.pop("purpose")
        response = client.post(
            "/submit",
            data=data,
            files=[("files", ("notes.txt", payload, "text/plain"))],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        match = re.search(r"/submission/([^?]+)", response.headers["location"])
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def _write_roster(self, rows: list[dict]) -> dict:
        document = normalize_document({"version": 1, "customers": rows})
        app_module.storage.save_roster_document(document, "Seed roster")
        return document

    def _roster(self) -> dict:
        return json.loads(self._roster_bytes().decode("utf-8"))

    def _roster_bytes(self) -> bytes:
        return (app_module.storage.local_root / "customers" / "roster.json").read_bytes()

    def _manifest(self, sub_id: str) -> dict:
        return json.loads((app_module.storage.local_root / sub_id / "manifest.json").read_text(encoding="utf-8"))

    def _card(self, html: str, sub_id: str) -> str:
        match = re.search(rf'<a class="feed-link" href="/submission/{re.escape(sub_id)}">.*?</a>', html, re.S)
        self.assertIsNotNone(match, sub_id)
        assert match is not None
        return match.group(0)

    def _write_legacy(self, sub_id: str, manifest: dict) -> Path:
        folder = app_module.storage.local_root / sub_id
        (folder / "files").mkdir(parents=True, exist_ok=True)
        (folder / "results").mkdir(exist_ok=True)
        for name in manifest.get("files") or []:
            (folder / "files" / name).write_bytes(b"legacy")
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _sheet(self, content: bytes) -> str:
        with zipfile.ZipFile(BytesIO(content)) as book:
            return book.read("xl/worksheets/sheet1.xml").decode("utf-8")

    def _name(self) -> str:
        self.seq += 1
        return f"測試顧客{self.seq}"

    def _phone(self) -> str:
        self.seq += 1
        return f"0000-{self.seq:04d}"


if __name__ == "__main__":
    unittest.main()

"""Bulk soft-delete for submissions, catalog products, and customers.

Uses generated passwords and fake records. Uploaded files stay on disk.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-bulk-"))
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
from catalog import merge_catalogs  # noqa: E402
from roster import merge_rosters, normalize_document  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
SELECT_FIRST = "請先選擇要刪除的項目。"


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


def _fields(csrf: str, ids: list[str], confirm: str = "") -> dict[str, object]:
    payload: dict[str, object] = {"csrf": csrf, "ids": ids}
    if confirm:
        payload["confirm"] = confirm
    return payload


class BulkDeleteTests(unittest.TestCase):
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

    def test_submission_bulk_delete_is_soft_and_admin_can_do_it(self) -> None:
        keep = self._submission(b"keep-file")
        first = self._submission(b"first-file")
        second = self._submission(b"second-file")
        before_keep = self._file(keep)
        before_first = self._file(first)
        debbie = self._login("Debbie")
        home = debbie.get("/")
        self.assertIn("批量刪除", home.text)
        self.assertIn('data-select-all', home.text)
        self.assertIn(f'value="{first}"', home.text)
        self.assertIn(f'value="{second}"', home.text)

        empty = debbie.post("/submissions/bulk-delete", data=_fields(_csrf(home.text), []))
        self.assertEqual(empty.status_code, 400)
        self.assertIn(SELECT_FIRST, empty.text)
        self.assertEqual(self._status(first), "pending")
        self.assertEqual(self._file(first), before_first)

        bad_csrf = debbie.post(
            "/submissions/bulk-delete",
            data=_fields("not-the-token", [first, second]),
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", bad_csrf.text)
        self.assertEqual(self._status(first), "pending")
        self.assertEqual(self._status(second), "pending")

        anon = TestClient(app_module.app)
        anon_post = anon.post(
            "/submissions/bulk-delete",
            data=_fields("missing", [first]),
            follow_redirects=False,
        )
        self.assertEqual(anon_post.status_code, 303)
        self.assertIn("/login", anon_post.headers["location"])

        preview = debbie.post(
            "/submissions/bulk-delete",
            data=_fields(_csrf(debbie.get("/").text), [first, second]),
        )
        self.assertEqual(preview.status_code, 200)
        phrase = "確定刪除已選的 2 筆？"
        self.assertIn(f"<h1>{phrase}</h1>", preview.text)
        self.assertIn(f'value="{phrase}"', preview.text)
        self.assertIn(first, preview.text)
        self.assertNotIn(keep, preview.text)
        self.assertEqual(self._status(first), "pending")

        rejected = debbie.post(
            "/submissions/bulk-delete",
            data=_fields(_csrf(preview.text), [first, second], "確定刪除"),
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("請先確認再刪除。", rejected.text)
        self.assertEqual(self._status(first), "pending")
        self.assertEqual(self._file(first), before_first)

        removed = debbie.post(
            "/submissions/bulk-delete",
            data=_fields(_csrf(preview.text), [first, second], phrase),
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        self.assertEqual(removed.headers["location"], "/?notice=bulk-deleted")
        home_after = debbie.get(removed.headers["location"])
        self.assertIn("已刪除所選提交，近期列表不會再顯示。", home_after.text)
        self.assertNotIn(first, self._listed())
        self.assertNotIn(second, self._listed())
        self.assertIn(keep, self._listed())
        self.assertEqual(self._status(first), "deleted")
        self.assertEqual(self._status(second), "deleted")
        self.assertEqual(self._manifest(first)["deleted_by"], "Debbie")
        self.assertEqual(self._manifest(second)["deleted_by"], "Debbie")
        self.assertEqual(self._file(first), before_first)
        self.assertEqual(self._file(second), b"second-file")
        self.assertEqual(self._file(keep), before_keep)
        self.assertEqual(self._status(keep), "pending")
        with self.assertRaises(PermissionError):
            app_module.storage.soft_delete_submission(keep, "Guest")
        self.assertEqual(self._status(keep), "pending")

    def test_catalog_bulk_delete_keeps_tombstones_and_known_price(self) -> None:
        apple = "測試蘋果"
        pear = "測試香梨"
        cup = "測試杯子"
        kept_price = "HK$33.00"
        self._write_catalog(
            [
                {"id": "p0000bd01", "name": apple, "price": "HK$11.00", "note": "假資料"},
                {"id": "p0000bd02", "name": pear, "price": "HK$22.00", "note": ""},
                {"id": "p0000bd03", "name": cup, "price": kept_price, "note": "留下"},
            ]
        )
        before = self._catalog_bytes()
        debbie = self._login("Debbie")
        page = debbie.get("/catalog")
        self.assertIn("批量刪除", page.text)
        self.assertIn("全選這頁", page.text)
        empty = debbie.post("/catalog/bulk-delete", data=_fields(_csrf(page.text), []))
        self.assertEqual(empty.status_code, 400)
        self.assertIn(SELECT_FIRST, empty.text)
        self.assertEqual(self._catalog_bytes(), before)

        bad_csrf = debbie.post(
            "/catalog/bulk-delete",
            data=_fields("not-the-token", ["p0000bd01", "p0000bd02"]),
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", bad_csrf.text)
        self.assertEqual(self._catalog_bytes(), before)

        preview = debbie.post(
            "/catalog/bulk-delete",
            data=_fields(_csrf(debbie.get("/catalog").text), ["p0000bd01", "p0000bd02"]),
        )
        phrase = "確定刪除已選的 2 筆？"
        self.assertEqual(preview.status_code, 200)
        self.assertIn(f"<h1>{phrase}</h1>", preview.text)
        self.assertIn(apple, preview.text)
        self.assertIn(pear, preview.text)
        self.assertNotIn(cup, preview.text)

        removed = debbie.post(
            "/catalog/bulk-delete",
            data=_fields(_csrf(preview.text), ["p0000bd01", "p0000bd02"], phrase),
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303, removed.text)
        self.assertIn("notice=bulk-deleted", removed.headers["location"])
        after = debbie.get("/catalog")
        self.assertIn("已刪除所選貨品。", debbie.get(removed.headers["location"]).text)
        self.assertIn(cup, after.text)
        self.assertIn(kept_price, after.text)
        self.assertNotIn(apple, after.text)
        self.assertNotIn(pear, after.text)
        stored = self._catalog()
        self.assertEqual([row["id"] for row in stored["products"]], ["p0000bd03"])
        self.assertEqual(stored["products"][0]["price"], kept_price)
        deleted_ids = {row["id"] for row in stored["deleted"]}
        self.assertEqual(deleted_ids, {"p0000bd01", "p0000bd02"})
        self.assertTrue(all(row["deleted_by"] == "Debbie" for row in stored["deleted"]))
        revived = merge_catalogs(
            stored,
            {
                "products": [
                    {"id": "p0000bd01", "name": apple, "price": "USD 9.00"},
                    {"name": pear, "price": "USD 8.00"},
                    {"id": "p0000bd03", "name": cup, "price": ""},
                ]
            },
        )
        self.assertEqual([row["id"] for row in revived["products"]], ["p0000bd03"])
        self.assertEqual(revived["products"][0]["price"], kept_price)
        with self.assertRaises(PermissionError):
            app_module.storage.delete_catalog_products("Guest", ["p0000bd03"])
        self.assertEqual(self._catalog()["products"][0]["price"], kept_price)

    def test_customer_bulk_delete_keeps_tombstones_and_known_phone(self) -> None:
        self._write_roster(
            [
                {"id": "c0000bd01", "name": "測試顧客甲", "phone": "0000-1111", "phone_source": "conversation"},
                {"id": "c0000bd02", "name": "測試顧客乙", "phone": "0000-2222", "phone_source": "conversation"},
                {"id": "c0000bd03", "name": "測試顧客丙", "phone": "0000-3333", "phone_source": "conversation"},
            ]
        )
        before = self._roster_bytes()
        debbie = self._login("Debbie")
        page = debbie.get("/customers")
        self.assertIn("批量刪除", page.text)
        empty = debbie.post("/customers/bulk-delete", data=_fields(_csrf(page.text), []))
        self.assertEqual(empty.status_code, 400)
        self.assertIn(SELECT_FIRST, empty.text)
        self.assertEqual(self._roster_bytes(), before)

        bad_csrf = debbie.post(
            "/customers/bulk-delete",
            data=_fields("not-the-token", ["c0000bd01", "c0000bd02"]),
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertEqual(self._roster_bytes(), before)

        preview = debbie.post(
            "/customers/bulk-delete",
            data=_fields(_csrf(debbie.get("/customers").text), ["c0000bd01", "c0000bd02"]),
        )
        phrase = "確定刪除已選的 2 筆？"
        self.assertEqual(preview.status_code, 200)
        self.assertIn(f"<h1>{phrase}</h1>", preview.text)
        self.assertIn("測試顧客甲", preview.text)
        self.assertIn("測試顧客乙", preview.text)

        removed = debbie.post(
            "/customers/bulk-delete",
            data=_fields(_csrf(preview.text), ["c0000bd01", "c0000bd02"], phrase),
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303, removed.text)
        stored = self._roster()
        self.assertEqual([row["id"] for row in stored["customers"]], ["c0000bd03"])
        self.assertEqual(stored["customers"][0]["phone"], "0000-3333")
        self.assertEqual({row["id"] for row in stored["deleted"]}, {"c0000bd01", "c0000bd02"})
        revived = merge_rosters(
            stored,
            {
                "customers": [
                    {"id": "c0000bd01", "name": "測試顧客甲", "phone": "0000-1111"},
                    {"id": "c0000bd02", "name": "測試顧客乙", "phone": ""},
                    {"id": "c0000bd03", "name": "測試顧客丙", "phone": ""},
                ]
            },
        )
        self.assertEqual([row["id"] for row in revived["customers"]], ["c0000bd03"])
        self.assertEqual(revived["customers"][0]["phone"], "0000-3333")
        with self.assertRaises(PermissionError):
            app_module.storage.delete_customers("Guest", ["c0000bd03"])
        self.assertEqual(self._roster()["customers"][0]["phone"], "0000-3333")

    def test_bulk_controls_stay_with_each_row(self) -> None:
        self._submission(b"layout-file")
        self._write_catalog(
            [{"id": "p0000lay1", "name": "測試蘋果", "price": "HK$00.00", "note": ""}]
        )
        self._write_roster(
            [{"id": "c0000lay1", "name": "測試顧客甲", "phone": "0000-1111", "phone_source": "conversation"}]
        )
        debbie = self._login("Debbie")
        pages = {
            "home": debbie.get("/").text,
            "catalog": debbie.get("/catalog").text,
            "customers": debbie.get("/customers").text,
        }
        for name, page in pages.items():
            self.assertIn('class="bulk-form"', page, name)
            self.assertRegex(page, r'class="bulk-bar"[\s\S]{0,500}全選這頁[\s\S]{0,300}批量刪除', name)
            rows = re.findall(r'<li class="feed-row">(.*?)</li>', page, re.S)
            self.assertTrue(rows, name)
            for row in rows:
                self.assertIn('class="row-check"', row)
                self.assertIn('class="feed-card"', row)
                self.assertLess(row.find("row-check"), row.find("feed-card"))
        home_cards = re.findall(r'<div class="feed-card">(.*?)</div>', pages["home"], re.S)
        self.assertTrue(home_cards)
        self.assertTrue(all("feed-link" in card and "feed-delete" in card for card in home_cards))
        css = (app_module.BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".bulk-form {\n  display: flex;", css)
        self.assertIn(".feed-card > .feed-link", css)
        self.assertNotIn("grid-column: 2", css)

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

    def _submission(self, payload: bytes) -> str:
        return app_module.storage.create_submission(
            user="Debbie",
            kind="待判斷",
            note="",
            files=[("notes.txt", payload)],
        )

    def _manifest(self, sub_id: str) -> dict:
        path = app_module.storage.local_root / sub_id / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _status(self, sub_id: str) -> str:
        return str(self._manifest(sub_id).get("status") or "")

    def _file(self, sub_id: str) -> bytes:
        return (app_module.storage.local_root / sub_id / "files" / "notes.txt").read_bytes()

    def _listed(self) -> set[str]:
        return {item["id"] for item in app_module.storage.list_submissions()}

    def _write_catalog(self, rows: list[dict]) -> None:
        document = {
            "version": 1,
            "updated_at": "2026-11-14T10:00:00+08:00",
            "updated_by": "scanner",
            "products": rows,
            "deleted": [],
        }
        folder = app_module.storage.local_root / "catalog"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "catalog.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def _catalog(self) -> dict:
        return json.loads(self._catalog_bytes().decode("utf-8"))

    def _catalog_bytes(self) -> bytes:
        return (app_module.storage.local_root / "catalog" / "catalog.json").read_bytes()

    def _write_roster(self, rows: list[dict]) -> None:
        document = normalize_document({"version": 1, "customers": rows})
        app_module.storage.save_roster_document(document, "Seed roster")

    def _roster(self) -> dict:
        return json.loads(self._roster_bytes().decode("utf-8"))

    def _roster_bytes(self) -> bytes:
        return (app_module.storage.local_root / "customers" / "roster.json").read_bytes()


if __name__ == "__main__":
    unittest.main()

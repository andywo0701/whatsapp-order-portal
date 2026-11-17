"""Catalog uploads, published prices, and the on-demand scan button.

Uses generated passwords, fake product names, and example prices. No real webhook.
"""

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

_TMP = Path(tempfile.mkdtemp(prefix="wa-portal-catalog-"))
os.environ["LOCAL_DATA_DIR"] = str(_TMP)
os.environ["HTTPS_ONLY"] = "false"
os.environ["SESSION_SECRET"] = secrets.token_hex(16)
os.environ.pop("HF_TOKEN", None)
os.environ.pop("HUGGINGFACE_HUB_TOKEN", None)
os.environ.pop("INBOX_DATASET", None)
os.environ.pop("HF_DATASET", None)
os.environ.pop("SCAN_WEBHOOK_URL", None)
os.environ.pop("SCAN_WEBHOOK_KEY", None)
os.environ["SITE_USERS"] = ""
os.environ.pop("SITE_USERNAME", None)
os.environ.pop("SITE_PASSWORD", None)

from fastapi.testclient import TestClient  # noqa: E402
from huggingface_hub.errors import EntryNotFoundError  # noqa: E402

import app as app_module  # noqa: E402
import scan as scan_module  # noqa: E402
from auth import STAFF  # noqa: E402
from catalog import apply_edit, catalog_xlsx_bytes, merge_catalogs  # noqa: E402
from roster import detect_purpose  # noqa: E402
from sync_inbox import validate_catalog_payload  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
ADMINS = ("Debbie", "Taya", "Katherine")
CATALOG_DELETE = "確定刪除此貨品？"
CATALOG_EDIT = "確定儲存此貨品？"
SUBMISSION_DELETE = "確定刪除此提交？"
WEBHOOK_URL = "https://scan.example.test/hook"
WEBHOOK_KEY = "test-scan-key-not-real"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fed4ef0000000049454e44ae426082"
)


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


def _clear_scan_env() -> None:
    os.environ.pop("SCAN_WEBHOOK_URL", None)
    os.environ.pop("SCAN_WEBHOOK_KEY", None)


class _FakeHub:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.commits: list[dict] = []

    def create_commit(self, **kwargs):
        if self.fail:
            raise RuntimeError("hub down")
        self.commits.append(kwargs)

    def upload_file(self, **_kwargs):
        raise AssertionError("catalog publish should use one dataset commit")


class _WebhookResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, _n: int = 0) -> bytes:
        return b""

    def __enter__(self) -> "_WebhookResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class CatalogAndScanTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._login_attempts.clear()
        scan_module.reset_scan_limits()
        _clear_scan_env()
        storage = app_module.storage
        storage.repo_id = ""
        storage.token = ""
        storage._api = None
        storage._ensured = False
        storage._password_records = None
        accounts = storage._accounts_file()
        if accounts.exists():
            accounts.unlink()
        self._remove_tree(storage.local_root / "catalog")
        _clear_bootstrap()
        self.passwords = {name: _password() for name in STAFF}
        os.environ["SITE_USERS"] = "\n".join(f"{name}:{self.passwords[name]}" for name in STAFF)

    def tearDown(self) -> None:
        _clear_bootstrap()
        _clear_scan_env()
        scan_module.reset_scan_limits()
        app_module.storage.repo_id = ""
        app_module.storage.token = ""
        app_module.storage._api = None
        app_module.storage._ensured = False

    def test_detect_purpose_uses_names_not_file_bytes(self) -> None:
        self.assertEqual(detect_purpose("auto", ["WhatsApp Chat with example.zip"]), "orders")
        self.assertEqual(detect_purpose("", ["notes.txt"]), "orders")
        self.assertEqual(detect_purpose("auto", ["貨品清單.zip"]), "orders")
        self.assertEqual(detect_purpose("auto", ["notes.xlsx"]), "orders")
        self.assertEqual(detect_purpose("auto", ["photo.jpg"]), "orders")
        self.assertEqual(detect_purpose("auto", ["貨品清單.xlsx"]), "catalog")
        self.assertEqual(detect_purpose("auto", ["price-list.docx", "chat.txt"]), "catalog")
        self.assertEqual(detect_purpose("customers", ["貨品清單.xlsx"]), "customers")
        self.assertEqual(detect_purpose("catalog", ["photo.jpg"]), "catalog")
        self.assertEqual(detect_purpose("orders", ["產品價目.csv"]), "orders")
        with self.assertRaises(ValueError):
            detect_purpose("secret", ["notes.txt"])

    def test_upload_form_does_not_ask_for_kind(self) -> None:
        home = self._login("Andy").get("/")
        form = home.text.split('action="/submit"', 1)[1].split("</form", 1)[0]
        self.assertNotIn('name="kind"', form)
        self.assertNotIn("<select", form)
        self.assertNotIn("新單", form)
        self.assertNotIn("舊單", form)
        self.assertRegex(form, r'name="purpose" value="auto" checked')
        self.assertIn("自動分辨", form)
        self.assertIn("落單資料", form)
        self.assertIn("客戶資料", form)
        self.assertIn("貨品清單", form)
        self.assertIn("WhatsApp 紀錄日期", form)
        self.assertIn(app_module.RECORD_DATE_HINT, home.text)
        self.assertIn("不指定", form)
        self.assertIn("單日", form)
        self.assertIn("日期範圍", form)
        self.assertIn('action="/scan"', home.text)
        self.assertIn("立即掃描", home.text)
        chosen = self._login("Taya").get("/?purpose=catalog")
        self.assertRegex(chosen.text, r'name="purpose" value="catalog" checked')

    def test_new_upload_omits_kind_and_auto_detects_purpose(self) -> None:
        andy = self._login("Andy")
        orders = self._upload(andy, {}, "notes.txt", b"fake chat export")
        manifest = self._manifest(orders)
        self.assertEqual(manifest["kind"], "待判斷")
        self.assertNotIn("新單", manifest["kind"])
        self.assertEqual(manifest["purpose"], "orders")
        self.assertNotIn("record_date_start", manifest)
        detail = andy.get(f"/submission/{orders}")
        self.assertIn("待判斷", detail.text)
        self.assertIn("落單資料", detail.text)
        self.assertIn('action="/scan"', detail.text)
        self.assertIn(f'name="submission_id" value="{orders}"', detail.text)

        catalog = self._upload(
            andy,
            {"record_mode": "day", "record_day": "2024-08-19"},
            "貨品清單.xlsx",
            b"not-a-real-workbook HK$99.99",
        )
        saved = self._manifest(catalog)
        self.assertEqual(saved["kind"], "待判斷")
        self.assertEqual(saved["purpose"], "catalog")
        self.assertEqual(saved["record_date_start"], "2024-08-19")
        self.assertEqual(saved["record_date_end"], "2024-08-19")
        self.assertEqual(saved["files"], ["貨品清單.xlsx"])
        catalog_page = andy.get("/catalog")
        self.assertIn("還沒有貨品清單", catalog_page.text)
        self.assertNotIn("HK$99.99", catalog_page.text)
        detail = andy.get(f"/submission/{catalog}")
        self.assertIn("不會從檔案讀取或編造價錢", detail.text)
        self.assertIn("貨品清單.xlsx", detail.text)

        plain_sheet = self._upload(andy, {}, "notes.xlsx", b"orders-export")
        self.assertEqual(self._manifest(plain_sheet)["purpose"], "orders")
        zipped = self._upload(andy, {}, "貨品清單.zip", b"PK fake chat")
        self.assertEqual(self._manifest(zipped)["purpose"], "orders")
        override = self._upload(andy, {"purpose": "customers"}, "貨品清單.xlsx", b"contacts")
        self.assertEqual(self._manifest(override)["purpose"], "customers")
        photos = self._upload(andy, {"purpose": "catalog"}, "apple.jpg", PNG)
        self.assertEqual(self._manifest(photos)["purpose"], "catalog")
        self.assertEqual(self._manifest(photos)["kind"], "待判斷")

        rejected = self._upload(andy, {"purpose": "secret"}, "notes.txt", b"nope", expect=400)
        self.assertIn("請選擇上載用途。", rejected)

    def test_old_manifests_keep_kind_and_purpose(self) -> None:
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
        changed = self._write_legacy(
            "20240202-090000-edited",
            {
                "id": "20240202-090000-edited",
                "submitted_at": "2024-02-02T09:00:00+08:00",
                "submitter": "Taya",
                "kind": "已改",
                "note": "",
                "files": [],
                "status": "pending",
                "status_label": "待處理",
                "result_files": [],
                "admin_note": "",
                "purpose": "orders",
                "record_date_start": "2024-02-02",
                "record_date_end": "2024-02-02",
            },
        )
        before_legacy = legacy.read_bytes()
        before_changed = changed.read_bytes()
        self.assertNotIn(b"purpose", before_legacy)
        debbie = self._login("Debbie")
        opened = debbie.get("/submission/20240115-090000-legacy")
        self.assertEqual(opened.status_code, 200)
        self.assertIn("新單", opened.text)
        self.assertIn("落單資料", opened.text)
        self.assertIn("已完成", opened.text)
        self.assertNotIn('action="/scan"', opened.text)
        edited = debbie.get("/submission/20240202-090000-edited")
        self.assertIn("已改", edited.text)
        self.assertIn("落單資料", edited.text)
        self.assertIn("2024-02-02", edited.text)
        self.assertIn('action="/scan"', edited.text)
        app_module.storage.list_submissions()
        self.assertEqual(legacy.read_bytes(), before_legacy)
        self.assertEqual(changed.read_bytes(), before_changed)

    def test_catalog_page_merges_published_prices_without_inventing(self) -> None:
        kept = "HK$00.00"
        renamed_price = "USD 0.00"
        apple = "測試蘋果"
        pear = "測試香梨"
        blanked = merge_catalogs(
            {"products": [{"id": "p0000aa01", "name": apple, "price": kept}]},
            {"updated_by": "scanner", "products": [{"name": apple, "price": ""}]},
        )
        self.assertEqual(blanked["products"][0]["price"], kept)
        replaced = merge_catalogs(
            {"products": [{"id": "p0000aa01", "name": apple, "price": kept}]},
            {"updated_by": "scanner", "products": [{"name": apple, "price": renamed_price}]},
        )
        self.assertEqual(replaced["products"][0]["price"], renamed_price)
        self.assertEqual(replaced["products"][0]["id"], "p0000aa01")
        blocked = merge_catalogs(
            {"products": [], "deleted": [{"id": "p0000aa01", "name": apple, "price": kept}]},
            {"products": [{"id": "p0000zz99", "name": apple, "price": renamed_price}]},
        )
        self.assertEqual(blocked["products"], [])
        with self.assertRaises(ValueError):
            validate_catalog_payload({"products": [{"name": apple, "price": 12}]})
        validate_catalog_payload({"products": [{"name": apple, "price": kept, "image": "apple.png", "note": ""}]})

        self._write_catalog(
            [
                {
                    "id": "p0000aa01",
                    "name": apple,
                    "price": kept,
                    "image": "apple.png",
                    "note": "假資料",
                    "updated_at": "2026-11-14T10:00:00+08:00",
                    "submission_ids": [],
                },
                {"name": pear, "price": 12.5, "note": "數字應被忽略"},
            ]
        )
        andy = self._login("Andy")
        page = andy.get("/catalog")
        self.assertEqual(page.status_code, 200)
        self.assertIn(apple, page.text)
        self.assertIn(kept, page.text)
        self.assertIn(pear, page.text)
        self.assertIn("尚缺", page.text)
        self.assertNotIn("12.5", page.text)
        self.assertNotIn(self.passwords["Andy"], page.text)
        found = andy.get("/catalog", params={"q": "HK$00"})
        self.assertIn(apple, found.text)
        self.assertNotIn(pear, found.text)
        missing = andy.get("/catalog", params={"q": "沒有這項"})
        self.assertIn("沒有符合的貨品", missing.text)
        detail = andy.get("/catalog/p0000aa01")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(kept, detail.text)
        self.assertIn("apple.png", detail.text)
        self.assertNotIn("<img", detail.text)
        workbook = andy.get("/catalog/catalog.xlsx")
        self.assertEqual(workbook.status_code, 200)
        self.assertIn("catalog.xlsx", workbook.headers["content-disposition"])
        sheet = self._sheet(workbook.content)
        self.assertIn("貨品名稱", sheet)
        self.assertIn(apple, sheet)
        self.assertIn(kept, sheet)
        self.assertIn("尚缺", sheet)
        self.assertNotIn("12.5", sheet)
        self.assertIn('name="貨品清單"', self._workbook_xml(workbook.content))

        anon = TestClient(app_module.app)
        for path in (
            "/catalog",
            "/catalog/catalog.xlsx",
            "/catalog/p0000aa01",
            "/catalog/p0000aa01/edit",
            "/catalog/p0000aa01/delete",
        ):
            blocked_page = anon.get(path, follow_redirects=False)
            self.assertEqual(blocked_page.status_code, 303, path)
            self.assertIn("/login", blocked_page.headers["location"])

        storage = app_module.storage
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._ensured = True
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("catalog/catalog.json is absent")):
            again = andy.get("/catalog")
        self.assertEqual(again.status_code, 200)
        self.assertIn(apple, again.text)
        self.assertIn(kept, again.text)

    def test_empty_catalog_when_dataset_has_no_folder(self) -> None:
        self.assertFalse((app_module.storage.local_root / "catalog").exists())
        andy = self._login("Andy")
        page = andy.get("/catalog")
        self.assertIn("還沒有貨品清單", page.text)
        self.assertIn('href="/?purpose=catalog#upload-purpose"', page.text)
        self.assertFalse((app_module.storage.local_root / "catalog").exists())
        downloaded = andy.get("/catalog/catalog.xlsx")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("貨品名稱", self._sheet(downloaded.content))
        self.assertFalse((app_module.storage.local_root / "catalog").exists())

        storage = app_module.storage
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._ensured = True
        remote = {
            "version": 1,
            "updated_by": "scanner",
            "products": [{"name": "測試蘋果", "price": "HK$00.00", "note": "遠端發布"}],
        }
        remote_path = Path(tempfile.mkstemp(suffix=".json")[1])
        remote_path.write_text(json.dumps(remote), encoding="utf-8")

        def _download(**kwargs):
            if kwargs.get("filename") == "catalog/catalog.json":
                return str(remote_path)
            raise EntryNotFoundError(str(kwargs.get("filename")))

        with patch("storage.hf_hub_download", side_effect=_download):
            published = andy.get("/catalog")
        self.assertIn("測試蘋果", published.text)
        self.assertIn("HK$00.00", published.text)
        self.assertFalse((storage.local_root / "catalog" / "catalog.json").exists())
        remote_path.unlink(missing_ok=True)

    def test_catalog_image_and_admin_cannot_delete(self) -> None:
        apple = "測試蘋果"
        price = "HK$00.00"
        uploader = self._login("Katherine")
        submission = self._upload(uploader, {"purpose": "catalog"}, "apple.png", PNG)
        self._write_catalog(
            [
                {
                    "id": "p0000bb01",
                    "name": apple,
                    "price": price,
                    "image": "apple.png",
                    "note": "",
                    "submission_ids": [submission],
                }
            ]
        )
        debbie = self._login("Debbie")
        detail = debbie.get("/catalog/p0000bb01")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(price, detail.text)
        self.assertIn("<img", detail.text)
        self.assertIn("/catalog/p0000bb01/image", detail.text)
        self.assertIn("/catalog/p0000bb01/edit", detail.text)
        self.assertIn("/catalog/p0000bb01/delete", detail.text)
        self.assertNotIn("沒有權限。", detail.text)
        image = debbie.get("/catalog/p0000bb01/image")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, PNG)
        anon = TestClient(app_module.app)
        hidden = anon.get("/catalog/p0000bb01/image", follow_redirects=False)
        self.assertEqual(hidden.status_code, 303)
        self.assertIn("/login", hidden.headers["location"])

        for username in ADMINS:
            with self.subTest(username=username):
                client = self._login(username)
                opened = client.get("/catalog/p0000bb01/delete")
                self.assertEqual(opened.status_code, 200)
                self.assertIn(CATALOG_DELETE, opened.text)
                self.assertNotIn("沒有權限。", opened.text)
                edit_page = client.get("/catalog/p0000bb01/edit")
                self.assertEqual(edit_page.status_code, 200)
                self.assertIn(CATALOG_EDIT, edit_page.text)
                submission_page = client.get(f"/submission/{submission}/delete")
                self.assertEqual(submission_page.status_code, 200)
                self.assertIn(SUBMISSION_DELETE, submission_page.text)
                self.assertNotIn("沒有權限。", submission_page.text)
        self.assertEqual(self._catalog()["products"][0]["price"], price)
        removed = debbie.post(
            "/catalog/p0000bb01/delete",
            data={"csrf": _csrf(debbie.get("/catalog/p0000bb01/delete").text), "confirm": CATALOG_DELETE},
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        stored = self._catalog()
        self.assertEqual(stored["products"], [])
        self.assertEqual(stored["deleted"][0]["deleted_by"], "Debbie")
        self.assertEqual(stored["deleted"][0]["price"], price)
        revived = merge_catalogs(stored, {"products": [{"id": "p0000bb01", "name": apple, "price": "USD 0.00"}]})
        self.assertEqual(revived["products"], [])
        self.assertEqual((app_module.storage.local_root / submission / "files" / "apple.png").read_bytes(), PNG)

    def test_super_admin_delete_publishes_first(self) -> None:
        apple = "測試蘋果"
        price = "HK$00.00"
        self._write_catalog([{"id": "p0000cc01", "name": apple, "price": price, "note": "假資料"}])
        before = self._catalog_bytes()
        andy = self._login("Andy")
        confirm = andy.get("/catalog/p0000cc01/delete")
        self.assertIn(CATALOG_DELETE, confirm.text)
        self.assertIn(price, confirm.text)
        rejected = andy.post(
            "/catalog/p0000cc01/delete",
            data={"csrf": _csrf(confirm.text), "confirm": "刪除"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("請先確認再刪除。", rejected.text)
        self.assertEqual(self._catalog_bytes(), before)

        storage = app_module.storage
        failing = _FakeHub(fail=True)
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._api = failing
        storage._ensured = True
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("catalog/catalog.json is absent")):
            failed = andy.post(
                "/catalog/p0000cc01/delete",
                data={"csrf": _csrf(andy.get("/catalog/p0000cc01/delete").text), "confirm": CATALOG_DELETE},
            )
        self.assertEqual(failed.status_code, 500)
        self.assertIn("暫時無法刪除，請稍後再試。", failed.text)
        self.assertEqual(self._catalog_bytes(), before)
        self.assertEqual(failing.commits, [])

        working = _FakeHub()
        storage._api = working
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("catalog/catalog.json is absent")):
            removed = andy.post(
                "/catalog/p0000cc01/delete",
                data={"csrf": _csrf(andy.get("/catalog/p0000cc01/delete").text), "confirm": CATALOG_DELETE},
                follow_redirects=False,
            )
        self.assertEqual(removed.status_code, 303, removed.text)
        self.assertEqual(removed.headers["location"], "/catalog?notice=deleted")
        after = andy.get(removed.headers["location"])
        self.assertIn("已刪除此貨品。", after.text)
        self.assertNotIn(apple, after.text)
        self.assertEqual(len(working.commits), 1)
        commit = working.commits[0]
        paths = {item.path_in_repo: item.path_or_fileobj for item in commit["operations"]}
        self.assertEqual(set(paths), {"catalog/catalog.json", "catalog/catalog.xlsx"})
        published = json.loads(paths["catalog/catalog.json"].decode("utf-8"))
        self.assertEqual(published["products"], [])
        self.assertEqual(published["deleted"][0]["deleted_by"], "Andy")
        self.assertEqual(published["deleted"][0]["name"], apple)
        self.assertEqual(published["deleted"][0]["price"], price)
        stored = self._catalog()
        self.assertEqual(stored["products"], [])
        self.assertEqual(stored["deleted"][0]["name"], apple)
        revived = merge_catalogs(stored, {"products": [{"name": apple, "price": "USD 0.00"}]})
        self.assertEqual(revived["products"], [])

    def test_catalog_edit_keeps_prices_options_and_permissions(self) -> None:
        apple = "測試蘋果"
        kept = "HK$00.00"
        size_price = "USD 0.00"
        base = {
            "products": [
                {
                    "id": "p0000ee01",
                    "name": apple,
                    "price": kept,
                    "source": "人手輸入",
                    "options": [
                        {"size": "大", "price": size_price},
                        {"size": "中", "color": "紅", "price": kept},
                    ],
                }
            ]
        }
        merged = merge_catalogs(
            base,
            {
                "updated_by": "scanner",
                "products": [
                    {
                        "name": apple,
                        "price": "",
                        "options": [
                            {"size": "大", "price": ""},
                            {"size": "細", "price": kept},
                        ],
                    }
                ],
            },
        )
        row = merged["products"][0]
        self.assertEqual(row["id"], "p0000ee01")
        self.assertEqual(row["price"], kept)
        self.assertEqual(row["source"], "manual")
        prices = {(item["size"], item["color"]): item["price"] for item in row["options"]}
        self.assertEqual(prices[("大", "")], size_price)
        self.assertEqual(prices[("中", "紅")], kept)
        self.assertEqual(prices[("細", "")], kept)
        omitted = merge_catalogs(base, {"products": [{"name": apple, "price": ""}]})
        self.assertEqual(omitted["products"][0]["options"][0]["price"], size_price)
        self.assertEqual(omitted["products"][0]["source"], "manual")
        replaced = merge_catalogs(base, {"products": [{"name": apple, "price": "HK$00.00"}]})
        self.assertEqual(replaced["products"][0]["price"], kept)
        self.assertTrue(replaced["products"][0]["options"])
        with self.assertRaises(ValueError):
            validate_catalog_payload({"products": [{"name": apple, "options": [{"size": "大", "price": 12}]}]})
        listed = merge_catalogs(
            {"products": []},
            {"products": [{"name": "測試杯子", "sizes": ["大", "中", "細"], "colors": ["紅"], "price": ""}]},
        )
        cup = listed["products"][0]
        self.assertTrue(cup["id"].startswith("p"))
        self.assertEqual([item["size"] for item in cup["options"] if item["size"]], ["大", "中", "細"])
        self.assertEqual([item["color"] for item in cup["options"] if item["color"]], ["紅"])
        self.assertEqual(cup["price"], "")

        by_debbie = apply_edit(base, "p0000ee01", "Debbie", apple, "", "", None)
        self.assertEqual(by_debbie["products"][0]["id"], "p0000ee01")
        self.assertEqual(by_debbie["products"][0]["name"], apple)
        self.assertEqual(by_debbie["products"][0]["price"], kept)
        self.assertEqual(by_debbie["products"][0]["source"], "manual")
        self.assertEqual(by_debbie["products"][0]["options"][0]["price"], size_price)
        self.assertEqual(by_debbie["updated_by"], "Debbie")
        cleared = apply_edit(
            base,
            "p0000ee01",
            "Andy",
            "測試蘋果改名",
            "",
            "假備註",
            [
                {
                    "size": "大",
                    "color": "",
                    "other": "",
                    "price": "",
                    "prev_size": "大",
                    "prev_color": "",
                    "prev_other": "",
                    "clear": True,
                },
                {"size": "S", "color": "", "other": "", "price": size_price, "prev_size": "", "prev_color": "", "prev_other": ""},
            ],
        )
        edited = cleared["products"][0]
        self.assertEqual(edited["id"], "p0000ee01")
        self.assertEqual(edited["name"], "測試蘋果改名")
        self.assertEqual(edited["price"], kept)
        self.assertEqual(edited["note"], "假備註")
        self.assertEqual(edited["source"], "manual")
        self.assertEqual([(item["size"], item["price"]) for item in edited["options"]], [("S", size_price)])
        blank_option = apply_edit(
            base,
            "p0000ee01",
            "Andy",
            apple,
            "",
            "",
            [{"size": "大", "price": "", "prev_size": "大", "prev_color": "", "prev_other": ""}],
        )
        self.assertEqual(blank_option["products"][0]["options"][0]["price"], size_price)
        self.assertEqual(blank_option["products"][0]["price"], kept)

        self._write_catalog(
            [
                {
                    "id": "p0000ee01",
                    "name": apple,
                    "price": kept,
                    "image": "apple.png",
                    "note": "",
                    "options": [{"size": "大", "color": "", "other": "", "price": size_price}],
                }
            ]
        )
        images = app_module.storage.local_root / "catalog" / "images"
        images.mkdir(parents=True, exist_ok=True)
        (images / "apple.png").write_bytes(PNG)
        before = self._catalog_bytes()
        andy = self._login("Andy")
        form = andy.get("/catalog/p0000ee01/edit")
        self.assertEqual(form.status_code, 200)
        self.assertIn(CATALOG_EDIT, form.text)
        self.assertIn("尺碼／規格", form.text)
        self.assertIn("顏色", form.text)
        self.assertIn('name="csrf"', form.text)
        self.assertNotIn("新單", form.text)
        bad_csrf = andy.post(
            "/catalog/p0000ee01/edit",
            data={"csrf": "not-the-token", "confirm": CATALOG_EDIT, "name": apple, "price": kept, "note": ""},
        )
        self.assertEqual(bad_csrf.status_code, 400)
        self.assertIn("請重新整理後再試。", bad_csrf.text)
        self.assertEqual(self._catalog_bytes(), before)
        unconfirmed = andy.post(
            "/catalog/p0000ee01/edit",
            data={"csrf": _csrf(form.text), "confirm": "儲存", "name": apple, "price": "", "note": "不該寫入"},
        )
        self.assertEqual(unconfirmed.status_code, 400)
        self.assertIn("請先確認再儲存。", unconfirmed.text)
        self.assertEqual(self._catalog_bytes(), before)

        for username in ADMINS:
            with self.subTest(username=username):
                client = self._login(username)
                opened = client.get("/catalog/p0000ee01/edit")
                self.assertEqual(opened.status_code, 200)
                self.assertIn(CATALOG_EDIT, opened.text)
                self.assertIn("尺碼／規格", opened.text)
                self.assertIn('name="csrf"', opened.text)
                self.assertNotIn("沒有權限。", opened.text)
                denied = client.post(
                    "/catalog/p0000ee01/edit",
                    data={"csrf": _csrf(opened.text), "confirm": "不儲存", "name": "不該改", "price": ""},
                )
                self.assertEqual(denied.status_code, 400)
                self.assertIn("請先確認再儲存。", denied.text)
        self.assertEqual(self._catalog()["products"][0]["name"], apple)
        self.assertEqual(self._catalog()["products"][0]["price"], kept)

        storage = app_module.storage
        failing = _FakeHub(fail=True)
        storage.repo_id = "example/inbox"
        storage.token = "test-token"
        storage._api = failing
        storage._ensured = True
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("catalog/catalog.json is absent")):
            failed = andy.post(
                "/catalog/p0000ee01/edit",
                data={
                    "csrf": _csrf(andy.get("/catalog/p0000ee01/edit").text),
                    "confirm": CATALOG_EDIT,
                    "name": apple,
                    "price": "",
                    "note": "失敗不寫入",
                    "option_size_0": "大",
                    "option_price_0": "",
                    "option_prev_size_0": "大",
                },
            )
        self.assertEqual(failed.status_code, 500)
        self.assertIn("暫時無法儲存，請稍後再試。", failed.text)
        self.assertEqual(self._catalog_bytes(), before)
        self.assertEqual(failing.commits, [])

        working = _FakeHub()
        storage._api = working
        with patch("storage.hf_hub_download", side_effect=EntryNotFoundError("catalog/catalog.json is absent")):
            saved = andy.post(
                "/catalog/p0000ee01/edit",
                data={
                    "csrf": _csrf(andy.get("/catalog/p0000ee01/edit").text),
                    "confirm": CATALOG_EDIT,
                    "name": apple,
                    "price": "",
                    "note": "假備註",
                    "option_size_0": "大",
                    "option_color_0": "",
                    "option_other_0": "",
                    "option_price_0": "",
                    "option_prev_size_0": "大",
                    "option_prev_color_0": "",
                    "option_prev_other_0": "",
                    "option_size_1": "M",
                    "option_color_1": "藍",
                    "option_price_1": kept,
                },
                follow_redirects=False,
            )
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(saved.headers["location"], "/catalog/p0000ee01?notice=saved")
        storage.repo_id = ""
        storage.token = ""
        detail = andy.get(saved.headers["location"])
        self.assertIn("已儲存此貨品。", detail.text)
        self.assertIn("人手輸入", detail.text)
        self.assertIn("大", detail.text)
        self.assertIn("M", detail.text)
        self.assertIn("藍", detail.text)
        self.assertIn(size_price, detail.text)
        self.assertIn(kept, detail.text)
        stored = self._catalog()["products"][0]
        self.assertEqual(stored["id"], "p0000ee01")
        self.assertEqual(stored["price"], kept)
        self.assertEqual(stored["source"], "manual")
        self.assertEqual(stored["note"], "假備註")
        self.assertEqual(
            [(item["size"], item["color"], item["price"]) for item in stored["options"]],
            [("大", "", size_price), ("M", "藍", kept)],
        )
        self.assertEqual(len(working.commits), 1)
        paths = {item.path_in_repo: item.path_or_fileobj for item in working.commits[0]["operations"]}
        self.assertEqual(set(paths), {"catalog/catalog.json", "catalog/catalog.xlsx"})
        published = json.loads(paths["catalog/catalog.json"].decode("utf-8"))
        self.assertEqual(published["products"][0]["id"], "p0000ee01")
        self.assertEqual(published["products"][0]["price"], kept)
        sheet = self._sheet(paths["catalog/catalog.xlsx"])
        self.assertIn("尺碼／規格", sheet)
        self.assertIn("顏色", sheet)
        self.assertIn("其他規格", sheet)
        self.assertIn("M", sheet)
        self.assertIn(kept, sheet)
        with zipfile.ZipFile(BytesIO(paths["catalog/catalog.xlsx"])) as book:
            self.assertEqual(book.read("xl/media/image1.png"), PNG)
            drawing = book.read("xl/drawings/drawing1.xml").decode("utf-8")
            self.assertIn("r:embed=", drawing)
            self.assertNotIn("example.test", sheet)
            self.assertNotIn("example.test", drawing)

        readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn(app_module.RECORD_DATE_HINT, readme)
        self.assertIn("尺碼／規格", readme)
        self.assertIn("顏色", readme)
        self.assertIn("單價", readme)

    def test_catalog_xlsx_embeds_stored_image_and_spec_columns(self) -> None:
        apple = "測試蘋果"
        pear = "測試香梨"
        kept = "HK$00.00"
        size_price = "USD 0.00"
        document = {
            "products": [
                {
                    "id": "p0000ff01",
                    "name": apple,
                    "image": "apple.png",
                    "price": kept,
                    "note": "假資料",
                    "options": [
                        {"size": "大", "price": size_price},
                        {"size": "", "color": "紅", "price": ""},
                        {"size": "", "color": "", "other": "禮盒", "price": kept},
                    ],
                },
                {"id": "p0000ff02", "name": pear, "image": "", "price": ""},
                {"id": "p0000ff03", "name": "測試網址", "image": "https://example.test/secret.png", "price": kept},
            ]
        }
        workbook = catalog_xlsx_bytes(document, {"apple.png": PNG, "https://example.test/secret.png": PNG})
        with zipfile.ZipFile(BytesIO(workbook)) as book:
            names = book.namelist()
            self.assertEqual(book.read("xl/media/image1.png"), PNG)
            self.assertEqual([name for name in names if name.startswith("xl/media/")], ["xl/media/image1.png"])
            sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertNotIn("example.test", sheet)
            self.assertNotIn("secret.png", sheet)
        for column in ("貨品名稱", "圖片", "尺碼／規格", "顏色", "其他規格", "價錢"):
            self.assertIn(column, sheet)
        self.assertIn(apple, sheet)
        self.assertIn("大", sheet)
        self.assertIn("紅", sheet)
        self.assertIn("禮盒", sheet)
        self.assertIn(size_price, sheet)
        self.assertIn(kept, sheet)
        self.assertIn("尚缺", sheet)
        self.assertIn(pear, sheet)
        self.assertNotIn("沒有", sheet)

    def test_scan_requires_login_csrf_and_is_limited(self) -> None:
        anon = TestClient(app_module.app)
        blocked = anon.post("/scan", data={"csrf": "x"}, follow_redirects=False)
        self.assertEqual(blocked.status_code, 303)
        self.assertIn("/login", blocked.headers["location"])

        andy = self._login("Andy")
        calls: list[tuple] = []

        def _fake_send(request, timeout):
            calls.append((request, timeout))
            return _WebhookResponse(204)

        with patch("scan._send", side_effect=_fake_send):
            offline = andy.post("/scan", data={"csrf": _csrf(andy.get("/").text)})
        self.assertEqual(offline.status_code, 200)
        self.assertIn("即時掃描還沒接上。可以在 Grok Bot 對話裡請它掃描。", offline.text)
        self.assertEqual(calls, [])
        self.assertNotIn("scan.example.test", offline.text)

        denied = andy.post("/scan", data={"csrf": "not-the-token"})
        self.assertEqual(denied.status_code, 400)
        self.assertIn("請重新整理後再試。", denied.text)
        self.assertNotIn(self.passwords["Andy"], denied.text)

        os.environ["SCAN_WEBHOOK_URL"] = "http://scan.example.test/hook"
        os.environ["SCAN_WEBHOOK_KEY"] = WEBHOOK_KEY
        scan_module.reset_scan_limits()
        with patch("scan._send", side_effect=_fake_send):
            insecure = andy.post("/scan", data={"csrf": _csrf(andy.get("/").text)})
        self.assertEqual(insecure.status_code, 200)
        self.assertIn("即時掃描還沒接上", insecure.text)
        self.assertNotIn("scan.example.test", insecure.text)
        self.assertNotIn(WEBHOOK_KEY, insecure.text)
        self.assertEqual(calls, [])

        os.environ["SCAN_WEBHOOK_URL"] = WEBHOOK_URL
        os.environ["SCAN_WEBHOOK_KEY"] = WEBHOOK_KEY
        scan_module.reset_scan_limits()
        submission = self._upload(andy, {}, "notes.txt", b"pending export")
        with patch("scan._send", side_effect=_fake_send):
            for _ in range(scan_module.SCAN_MAX):
                sent = andy.post(
                    "/scan",
                    data={"csrf": _csrf(andy.get("/").text)},
                    follow_redirects=False,
                )
                self.assertEqual(sent.status_code, 303, sent.text)
                self.assertEqual(sent.headers["location"], "/?notice=scan-sent")
                self.assertNotIn(WEBHOOK_KEY, sent.headers["location"])
            notice = andy.get(sent.headers["location"])
            self.assertIn("已發出掃描要求，做好會把 Excel 放回這裡。", notice.text)
            self.assertNotIn(WEBHOOK_KEY, notice.text)
            self.assertNotIn("scan.example.test", notice.text)
            detail = andy.post(
                "/scan",
                data={
                    "csrf": _csrf(andy.get(f"/submission/{submission}").text),
                    "submission_id": submission,
                },
                follow_redirects=False,
            )
        self.assertEqual(detail.status_code, 429)
        self.assertIn("掃描要求太頻密，請稍候再按。", detail.text)
        self.assertEqual(len(calls), scan_module.SCAN_MAX)
        request, timeout = calls[0]
        self.assertEqual(timeout, scan_module.SCAN_TIMEOUT)
        self.assertEqual(request.full_url, WEBHOOK_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {WEBHOOK_KEY}")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["action"], "scan")
        self.assertEqual(body["source"], "whatsapp-order-portal")
        self.assertEqual(body["requested_by"], "Andy")
        self.assertNotIn("submission_id", body)
        self.assertNotIn(WEBHOOK_KEY, request.data.decode("utf-8"))

        scan_module.reset_scan_limits()
        calls.clear()
        with patch("scan._send", side_effect=_fake_send):
            aimed = andy.post(
                "/scan",
                data={
                    "csrf": _csrf(andy.get(f"/submission/{submission}").text),
                    "submission_id": submission,
                },
                follow_redirects=False,
            )
        self.assertEqual(aimed.status_code, 303)
        self.assertIn(submission, aimed.headers["location"])
        self.assertIn("notice=scan-sent", aimed.headers["location"])
        aimed_body = json.loads(calls[0][0].data.decode("utf-8"))
        self.assertEqual(aimed_body["submission_id"], submission)
        self.assertNotIn(WEBHOOK_KEY, aimed.headers["location"])

        scan_module.reset_scan_limits()
        debbie = self._login("Debbie")
        with patch("scan._send", return_value=_WebhookResponse(500)):
            failed = debbie.post("/scan", data={"csrf": _csrf(debbie.get("/").text)})
        self.assertEqual(failed.status_code, 200)
        self.assertIn("暫時無法發出掃描要求", failed.text)
        self.assertNotIn(WEBHOOK_KEY, failed.text)
        self.assertNotIn(WEBHOOK_URL, failed.text)

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

    def _upload(self, client: TestClient, fields: dict[str, str], filename: str, payload: bytes, expect: int = 303):
        home = client.get("/")
        data = {
            "csrf": _csrf(home.text),
            "note": "",
            "record_mode": fields.get("record_mode", ""),
            "record_day": fields.get("record_day", ""),
            "record_date_start": fields.get("record_date_start", ""),
            "record_date_end": fields.get("record_date_end", ""),
        }
        if "purpose" in fields:
            data["purpose"] = fields["purpose"]
        if "kind" in fields:
            data["kind"] = fields["kind"]
        response = client.post(
            "/submit",
            data=data,
            files=[("files", (filename, payload, "application/octet-stream"))],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, expect, response.text)
        if expect != 303:
            return response.text
        match = re.search(r"/submission/([^?]+)", response.headers["location"])
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

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

    def _manifest(self, sub_id: str) -> dict:
        return json.loads((app_module.storage.local_root / sub_id / "manifest.json").read_text(encoding="utf-8"))

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

    def _workbook_xml(self, content: bytes) -> str:
        with zipfile.ZipFile(BytesIO(content)) as book:
            return book.read("xl/workbook.xml").decode("utf-8")

    def _remove_tree(self, folder: Path) -> None:
        if not folder.exists():
            return
        for path in sorted(folder.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        folder.rmdir()


if __name__ == "__main__":
    unittest.main()

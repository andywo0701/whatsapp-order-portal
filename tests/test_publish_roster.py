"""Official roster publish used by the scanner. Fake numbers only."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from huggingface_hub.errors import EntryNotFoundError

import sync_inbox
from roster import phone_key

ID_RE = re.compile(r"^c[0-9a-f]{8}$")


class _FakeApi:
    def __init__(self, token: str = "", fail: bool = False) -> None:
        self.token = token
        self.fail = fail
        self.commits: list[dict] = []

    def create_commit(self, **kwargs):
        if self.fail:
            raise RuntimeError("hub down")
        self.commits.append(kwargs)

    def upload_file(self, **_kwargs):
        raise AssertionError("roster publish must be one commit, not upload_file")


class PublishRosterTests(unittest.TestCase):
    def test_merge_keeps_phone_maps_status_and_assigns_ids(self) -> None:
        official = {
            "version": 1,
            "updated_by": "scanner",
            "customers": [
                {
                    "id": "c0000aa01",
                    "name": "測試顧客甲",
                    "phone": "0000-1111",
                    "phone_source": "conversation",
                    "status": "ok",
                }
            ],
            "deleted": [
                {
                    "id": "c0000aa09",
                    "name": "測試顧客丙",
                    "phone": "0000-3333",
                    "phones": ["0000-3333"],
                }
            ],
        }
        incoming = {
            "version": 1,
            "updated_by": "scanner",
            "customers": [
                {"name": "測試顧客甲", "phone": "", "status": "ok"},
                {"name": "測試顧客乙", "phone": "0000-2222", "status": "need_info"},
                {"name": "測試顧客丁", "phone": "0000-4444", "狀態": "待補資料"},
                {"name": "沒有編號", "phone": "0000-5555"},
                {"id": "c0000aa09", "name": "測試顧客丙", "phone": "0000-3333", "status": "ok"},
                {"name": "測試顧客甲", "phone": "0000-9999", "status": "ok"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            official_path = folder / "official.json"
            official_path.write_text(json.dumps(official, ensure_ascii=False), encoding="utf-8")
            incoming_path = folder / "incoming.json"
            incoming_path.write_text(json.dumps(incoming, ensure_ascii=False), encoding="utf-8")
            before = official_path.read_bytes()
            api = _FakeApi()

            def download(**kwargs):
                self.assertEqual(kwargs["filename"], "customers/roster.json")
                self.assertEqual(kwargs["repo_type"], "dataset")
                return str(official_path)

            with patch("sync_inbox.token", return_value="test-token"), patch(
                "sync_inbox.hf_hub_download", side_effect=download
            ), patch("sync_inbox.HfApi", return_value=api):
                sync_inbox.publish_roster(incoming_path)

            self.assertEqual(official_path.read_bytes(), before)
            self.assertEqual(len(api.commits), 1)
            commit = api.commits[0]
            paths = {item.path_in_repo: item.path_or_fileobj for item in commit["operations"]}
            self.assertEqual(set(paths), {"customers/roster.json", "customers/roster.xlsx"})
            published = json.loads(paths["customers/roster.json"].decode("utf-8"))
            by_name = {row["name"]: row for row in published["customers"]}
            kept = by_name["測試顧客甲"]
            self.assertIn(phone_key("0000-1111"), {phone_key(item["phone"]) for item in kept["phones"]})
            self.assertIn(phone_key("0000-9999"), {phone_key(item["phone"]) for item in kept["phones"]})
            self.assertEqual(kept["status"], "pending")
            self.assertEqual(by_name["測試顧客乙"]["status"], "pending")
            self.assertEqual(by_name["測試顧客乙"]["phone"], "0000-2222")
            self.assertEqual(by_name["測試顧客丁"]["status"], "pending")
            fresh = by_name["沒有編號"]
            self.assertRegex(fresh["id"], ID_RE)
            self.assertNotIn("測試顧客丙", by_name)
            self.assertIn("c0000aa09", {row.get("id") for row in published["deleted"]})
            sheet = paths["customers/roster.xlsx"]
            self.assertIsInstance(sheet, bytes)
            with zipfile.ZipFile(BytesIO(sheet)) as book:
                xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("0000-1111", xml)

    def test_failed_commit_leaves_the_official_roster_unchanged(self) -> None:
        official = {
            "version": 1,
            "customers": [
                {"id": "c0000aa01", "name": "測試顧客甲", "phone": "0000-1111", "status": "ok"}
            ],
            "deleted": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            official_path = folder / "official.json"
            official_path.write_text(json.dumps(official), encoding="utf-8")
            incoming_path = folder / "incoming.json"
            incoming_path.write_text(
                json.dumps({"customers": [{"name": "測試顧客甲", "phone": ""}]}),
                encoding="utf-8",
            )
            before = official_path.read_bytes()
            remote = {"customers/roster.json": before}

            def download(**_kwargs):
                return str(official_path)

            def create_commit(**kwargs):
                raise RuntimeError("hub down")

            api = _FakeApi(fail=True)
            api.create_commit = create_commit  # type: ignore[method-assign]
            with patch("sync_inbox.token", return_value="test-token"), patch(
                "sync_inbox.hf_hub_download", side_effect=download
            ), patch("sync_inbox.HfApi", return_value=api):
                with self.assertRaises(RuntimeError):
                    sync_inbox.publish_roster(incoming_path)
            self.assertEqual(remote["customers/roster.json"], before)
            self.assertEqual(official_path.read_bytes(), before)

    def test_missing_official_roster_still_assigns_an_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            incoming_path = Path(tmp) / "incoming.json"
            incoming_path.write_text(
                json.dumps(
                    {"customers": [{"name": "測試顧客乙", "phone": "0000-2222", "status": "need_info"}]}
                ),
                encoding="utf-8",
            )
            api = _FakeApi()

            def download(**_kwargs):
                raise EntryNotFoundError("customers/roster.json is absent")

            with patch("sync_inbox.token", return_value="test-token"), patch(
                "sync_inbox.hf_hub_download", side_effect=download
            ), patch("sync_inbox.HfApi", return_value=api):
                sync_inbox.publish_roster(incoming_path)
            published = json.loads(api.commits[0]["operations"][0].path_or_fileobj.decode("utf-8"))
            row = published["customers"][0]
            self.assertEqual(row["status"], "pending")
            self.assertRegex(row["id"], ID_RE)
            self.assertEqual(row["phone"], "0000-2222")


if __name__ == "__main__":
    os.environ.setdefault("HF_WRITE_TOKEN", "")
    unittest.main()

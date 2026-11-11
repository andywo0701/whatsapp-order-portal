from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

HK = ZoneInfo("Asia/Hong_Kong")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._\u4e00-\u9fff\-()+ ]+$")


class Storage:
    def __init__(self) -> None:
        self.repo_id = os.environ.get("INBOX_DATASET") or os.environ.get("HF_DATASET") or ""
        self.token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or ""
        self.local_root = Path(os.environ.get("LOCAL_DATA_DIR") or (Path(__file__).resolve().parent / "local_data"))
        self.local_root.mkdir(parents=True, exist_ok=True)
        self._api: HfApi | None = None
        self._ensured = False

    def is_ready(self) -> bool:
        return bool(self.repo_id and self.token)

    def _api_client(self) -> HfApi:
        if self._api is None:
            self._api = HfApi(token=self.token)
        return self._api

    def ensure_repo(self) -> None:
        if self._ensured or not self.is_ready():
            return
        api = self._api_client()
        try:
            api.repo_info(self.repo_id, repo_type="dataset")
        except RepositoryNotFoundError:
            api.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
            readme = (
                "---\nlicense: other\n---\n\n"
                "Private inbox for WhatsApp order uploads. Do not make this dataset public.\n"
            )
            api.upload_file(
                path_or_fileobj=readme.encode("utf-8"),
                path_in_repo="README.md",
                repo_id=self.repo_id,
                repo_type="dataset",
                commit_message="Initialize private inbox",
            )
        self._ensured = True

    def _local_dir(self, sub_id: str) -> Path:
        path = self.local_root / sub_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "files").mkdir(exist_ok=True)
        (path / "results").mkdir(exist_ok=True)
        return path

    def create_submission(
        self, user: str, kind: str, note: str, files: list[tuple[str, bytes]]
    ) -> str:
        now = datetime.now(HK)
        sub_id = now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        used: set[str] = set()
        stored_names: list[str] = []
        local = self._local_dir(sub_id)
        for original, data in files:
            name = Path(original).name
            stem, ext = Path(name).stem, Path(name).suffix
            candidate = name
            i = 2
            while candidate.lower() in used:
                candidate = f"{stem}_{i}{ext}"
                i += 1
            used.add(candidate.lower())
            (local / "files" / candidate).write_bytes(data)
            stored_names.append(candidate)
        manifest = {
            "id": sub_id,
            "submitted_at": now.isoformat(),
            "submitter": user,
            "kind": kind,
            "note": note,
            "files": stored_names,
            "status": "pending",
            "status_label": "待處理",
            "result_files": [],
            "admin_note": "",
        }
        (local / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.ensure_repo()
        if self.is_ready():
            self._upload_submission(sub_id, local)
        return sub_id

    def _upload_submission(self, sub_id: str, local: Path) -> None:
        api = self._api_client()
        prefix = f"inbox/{sub_id}"
        api.upload_file(
            path_or_fileobj=(local / "manifest.json").read_bytes(),
            path_in_repo=f"{prefix}/manifest.json",
            repo_id=self.repo_id,
            repo_type="dataset",
            commit_message=f"Add submission {sub_id}",
        )
        for path in sorted((local / "files").iterdir()):
            if path.is_file():
                api.upload_file(
                    path_or_fileobj=path.read_bytes(),
                    path_in_repo=f"{prefix}/files/{path.name}",
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    commit_message=f"Add file for {sub_id}",
                )

    def _load_local_manifest(self, sub_id: str) -> dict[str, Any] | None:
        path = self.local_root / sub_id / "manifest.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _refresh_from_hub(self, sub_id: str) -> dict[str, Any] | None:
        if not self.is_ready() or not SAFE_ID.fullmatch(sub_id):
            return None
        try:
            downloaded = hf_hub_download(
                repo_id=self.repo_id,
                repo_type="dataset",
                filename=f"inbox/{sub_id}/manifest.json",
                token=self.token,
            )
            data = json.loads(Path(downloaded).read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        local = self._local_dir(sub_id)
        (local / "manifest.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in data.get("result_files") or []:
            if not isinstance(name, str) or not SAFE_NAME.fullmatch(Path(name).name):
                continue
            dest = local / "results" / Path(name).name
            if dest.exists():
                continue
            try:
                got = hf_hub_download(
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    filename=f"inbox/{sub_id}/results/{Path(name).name}",
                    token=self.token,
                )
                dest.write_bytes(Path(got).read_bytes())
            except Exception:
                continue
        return data

    def get_submission(self, sub_id: str) -> dict[str, Any] | None:
        if not SAFE_ID.fullmatch(sub_id):
            return None
        item = self._refresh_from_hub(sub_id) or self._load_local_manifest(sub_id)
        return self._present(item) if item else None

    def list_submissions(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.local_root.exists():
            for path in self.local_root.iterdir():
                if path.is_dir() and (path / "manifest.json").exists() and SAFE_ID.fullmatch(path.name):
                    item = self._load_local_manifest(path.name)
                    if item:
                        found[path.name] = item
        if self.is_ready():
            try:
                api = self._api_client()
                tree = api.list_repo_tree(
                    self.repo_id, repo_type="dataset", path_in_repo="inbox", recursive=True
                )
                ids = set()
                for entry in tree:
                    path = getattr(entry, "path", "") or ""
                    parts = path.split("/")
                    if len(parts) >= 2 and parts[0] == "inbox" and SAFE_ID.fullmatch(parts[1]):
                        ids.add(parts[1])
                for sub_id in ids:
                    if sub_id not in found:
                        item = self.get_submission(sub_id)
                        if item:
                            found[sub_id] = item
                    else:
                        refreshed = self._refresh_from_hub(sub_id)
                        if refreshed:
                            found[sub_id] = refreshed
            except Exception:
                pass
        items = [self._present(v) for v in found.values()]
        items.sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
        return items

    def local_file_path(self, sub_id: str, kind: str, filename: str) -> Path | None:
        if not SAFE_ID.fullmatch(sub_id) or kind not in {"files", "results"}:
            return None
        name = Path(filename).name
        if not name or not SAFE_NAME.fullmatch(name):
            return None
        path = self.local_root / sub_id / kind / name
        if path.is_file():
            return path
        if kind == "results":
            self._refresh_from_hub(sub_id)
            if path.is_file():
                return path
        if kind == "files" and self.is_ready():
            try:
                got = hf_hub_download(
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    filename=f"inbox/{sub_id}/files/{name}",
                    token=self.token,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(got).read_bytes())
                return path if path.is_file() else None
            except Exception:
                return None
        return None

    def _present(self, item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        status = str(out.get("status") or "pending")
        labels = {
            "pending": "待處理",
            "processing": "處理中",
            "done": "已完成",
            "need_info": "待補資料",
        }
        out["status"] = status
        out["status_label"] = out.get("status_label") or labels.get(status, status)
        out["files"] = [Path(str(n)).name for n in (out.get("files") or []) if n]
        out["result_files"] = [Path(str(n)).name for n in (out.get("result_files") or []) if n]
        out["note"] = out.get("note") or ""
        out["admin_note"] = out.get("admin_note") or ""
        submitted = out.get("submitted_at") or ""
        out["submitted_display"] = self._format_time(submitted)
        return out

    def _format_time(self, value: str) -> str:
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(value)
            return dt.astimezone(HK).strftime("%Y-%m-%d %H:%M") + " HKT"
        except Exception:
            return value

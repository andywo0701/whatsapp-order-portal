from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

from auth import can_manage
from catalog import (
    CATALOG_JSON_PATH,
    CATALOG_XLSX_PATH,
    CatalogNotFound,
    apply_delete as apply_catalog_delete,
    apply_edit as apply_catalog_edit,
    catalog_xlsx_bytes,
    clean_image,
    empty_document as empty_catalog,
    image_is_displayable,
    merge_catalogs,
    parse_catalog_text,
)
from roster import (
    PURPOSE_LABELS,
    ROSTER_JSON_PATH,
    ROSTER_XLSX_PATH,
    RosterNotFound,
    apply_confirm,
    apply_delete,
    apply_edit,
    canonical_purpose,
    empty_document,
    merge_rosters,
    parse_roster_text,
    purpose_of,
    roster_xlsx_bytes,
)

HK = ZoneInfo("Asia/Hong_Kong")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._\u4e00-\u9fff\-()+ ]+$")
ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ACCOUNTS_REPO_PATH = "users/accounts.json"
KIND_PENDING = "待判斷"
CHUNK_TTL_SECONDS = 24 * 60 * 60
INLINE_PUBLISH_BYTES = 80 * 1024 * 1024
_UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")


class ChunkUploadError(ValueError):
    """A chunked upload the page can explain to staff."""
# Posted by older forms, or later by the scanning bot. The upload form no longer asks.
LEGACY_KINDS = {"新單", "改單", "取消", "已改", "已取消", "聯絡人截圖", "其他", KIND_PENDING}


def canonical_kind(value: object) -> str:
    """Empty or unknown kinds stay 待判斷. The portal does not decide 新單 or 改單."""
    if isinstance(value, str):
        text = value.strip()
        if text in LEGACY_KINDS:
            return text
    return KIND_PENDING


def parse_iso_date(value: object) -> date | None:
    """Parse a calendar date. Blank is None. Anything else invalid raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid record date")
    text = value.strip()
    if not text:
        return None
    if not ISO_DATE.fullmatch(text):
        raise ValueError("invalid record date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid record date") from exc


def record_date_text(value: object) -> str:
    """YYYY-MM-DD when the value is a real date, otherwise empty. Does not invent one."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = parse_iso_date(value)
    except ValueError:
        return ""
    return parsed.isoformat() if parsed else ""


def canonical_record_dates(
    start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Normalize an optional window. One chosen day is stored as the same start and end."""
    start_day = parse_iso_date(start)
    end_day = parse_iso_date(end)
    if start_day is None and end_day is None:
        return None, None
    if start_day is None and end_day is not None:
        iso = end_day.isoformat()
        return iso, iso
    if end_day is None and start_day is not None:
        iso = start_day.isoformat()
        return iso, iso
    assert start_day is not None and end_day is not None
    if end_day < start_day:
        raise ValueError("record date end before start")
    return start_day.isoformat(), end_day.isoformat()


def record_date_label(start: object, end: object) -> str:
    start_text = record_date_text(start)
    end_text = record_date_text(end)
    if start_text and end_text:
        if start_text == end_text:
            return start_text
        return f"{start_text} 至 {end_text}"
    return start_text or end_text


def manifest_deleted(item: dict[str, Any]) -> bool:
    """True when this manifest was soft-deleted. Missing fields are not deleted."""
    if not isinstance(item, dict):
        return False
    if str(item.get("status") or "") == "deleted":
        return True
    deleted_at = item.get("deleted_at")
    return isinstance(deleted_at, str) and bool(deleted_at.strip())


class Storage:
    def __init__(self) -> None:
        self.repo_id = os.environ.get("INBOX_DATASET") or os.environ.get("HF_DATASET") or ""
        self.token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or ""
        self.local_root = Path(os.environ.get("LOCAL_DATA_DIR") or (Path(__file__).resolve().parent / "local_data"))
        self.local_root.mkdir(parents=True, exist_ok=True)
        self._api: HfApi | None = None
        self._ensured = False
        self._password_records: dict[str, dict[str, Any]] | None = None
        self._chunk_guard = threading.Lock()
        self._chunk_locks: dict[str, threading.Lock] = {}
        self._publish_lock = threading.Lock()

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
        self,
        user: str,
        kind: str,
        note: str,
        files: list[tuple[str, bytes]],
        record_date_start: str | None = None,
        record_date_end: str | None = None,
        purpose: str | None = None,
    ) -> str:
        return self._create_submission(
            user, kind, note, list(files), record_date_start, record_date_end, purpose
        )

    def create_submission_from_paths(
        self,
        user: str,
        kind: str,
        note: str,
        files: list[tuple[str, Path]],
        record_date_start: str | None = None,
        record_date_end: str | None = None,
        purpose: str | None = None,
    ) -> str:
        """Move files into the inbox without reading them into memory."""
        return self._create_submission(
            user, kind, note, list(files), record_date_start, record_date_end, purpose
        )

    def _create_submission(
        self,
        user: str,
        kind: str,
        note: str,
        files: list[tuple[str, bytes | Path]],
        record_date_start: str | None,
        record_date_end: str | None,
        purpose: str | None,
    ) -> str:
        # Validate before creating a folder so a bad window never becomes a submission.
        start, end = canonical_record_dates(record_date_start, record_date_end)
        chosen_purpose = canonical_purpose(purpose)
        chosen_kind = canonical_kind(kind)
        now = datetime.now(HK)
        sub_id = now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        used: set[str] = set()
        stored_names: list[str] = []
        local = self._local_dir(sub_id)
        total = 0
        for original, source in files:
            name = Path(original).name
            stem, ext = Path(name).stem, Path(name).suffix
            candidate = name
            i = 2
            while candidate.lower() in used:
                candidate = f"{stem}_{i}{ext}"
                i += 1
            used.add(candidate.lower())
            total += self._place_upload(source, local / "files" / candidate)
            stored_names.append(candidate)
        manifest = {
            "id": sub_id,
            "submitted_at": now.isoformat(),
            "submitter": user,
            "kind": chosen_kind,
            "note": note,
            "files": stored_names,
            "status": "pending",
            "status_label": "待處理",
            "result_files": [],
            "admin_note": "",
            "purpose": chosen_purpose,
        }
        if start is not None and end is not None:
            manifest["record_date_start"] = start
            manifest["record_date_end"] = end
        (local / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(local / "manifest.json", 0o600)
        self._schedule_publish(sub_id, local, total)
        return sub_id

    def _place_upload(self, source: bytes | Path, dest: Path) -> int:
        """Put source at dest. A path is moved or streamed, never read whole."""
        if isinstance(source, (bytes, bytearray)):
            dest.write_bytes(source)
            os.chmod(dest, 0o600)
            return len(source)
        src = Path(source)
        try:
            os.replace(src, dest)
        except OSError:
            with src.open("rb") as incoming, dest.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            src.unlink(missing_ok=True)
        os.chmod(dest, 0o600)
        return dest.stat().st_size

    def _schedule_publish(self, sub_id: str, local: Path, total_bytes: int) -> None:
        if not self.is_ready():
            return
        self.ensure_repo()
        if total_bytes <= INLINE_PUBLISH_BYTES:
            self._upload_submission(sub_id, local)
            return
        threading.Thread(
            target=self._publish_quiet,
            args=(sub_id,),
            name=f"publish-{sub_id}",
            daemon=True,
        ).start()

    def _publish_quiet(self, sub_id: str) -> None:
        try:
            with self._publish_lock:
                self._upload_submission(sub_id, self._local_dir(sub_id))
        except Exception:
            sys.stderr.write(f"Dataset publish failed for {sub_id}.\n")

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
                    path_or_fileobj=str(path.resolve()),
                    path_in_repo=f"{prefix}/files/{path.name}",
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    commit_message=f"Add file for {sub_id}",
                )

    def cleanup_chunk_uploads(self, now: float | None = None) -> None:
        """Delete incomplete chunk sessions that have expired."""
        root = self.local_root / ".chunk-uploads"
        if not root.is_dir():
            return
        moment = time.time() if now is None else now
        for child in list(root.iterdir()):
            if not child.is_dir():
                continue
            expired = True
            meta_path = child / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    created = float(meta.get("created_at") or 0)
                    expired = moment - created > CHUNK_TTL_SECONDS
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    expired = True
            if expired:
                shutil.rmtree(child, ignore_errors=True)

    def begin_chunk_upload(
        self,
        user: str,
        *,
        note: str,
        kind: str,
        purpose: str,
        record_start: str | None,
        record_end: str | None,
        files: list[dict[str, Any]],
        chunk_bytes: int,
        max_file_bytes: int,
        max_total_bytes: int,
        max_files: int,
        file_too_big: str,
        total_too_big: str,
    ) -> dict[str, Any]:
        self.cleanup_chunk_uploads()
        if not files:
            raise ChunkUploadError("請至少選擇一個檔案。")
        if len(files) > max_files:
            raise ChunkUploadError(f"一次最多 {max_files} 個檔案。")
        total = 0
        normalized: list[dict[str, Any]] = []
        for item in files:
            name = item.get("name")
            size = item.get("size")
            if not isinstance(name, str) or not name or not isinstance(size, int) or isinstance(size, bool):
                raise ChunkUploadError("檔案資料不正確。")
            if size <= 0:
                raise ChunkUploadError("請至少選擇一個有內容的檔案。")
            if size > max_file_bytes:
                raise ChunkUploadError(file_too_big.format(name=name))
            total += size
            if total > max_total_bytes:
                raise ChunkUploadError(total_too_big)
            normalized.append({"name": name, "size": size, "received": []})
        upload_id = secrets.token_hex(16)
        folder = self._chunk_dir(upload_id)
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "parts").mkdir()
        meta = {
            "id": upload_id,
            "user": user,
            "created_at": time.time(),
            "note": note,
            "kind": kind,
            "purpose": purpose,
            "record_start": record_start,
            "record_end": record_end,
            "chunk_bytes": int(chunk_bytes),
            "files": normalized,
        }
        self._write_chunk_meta(folder, meta)
        return self._public_chunk_status(meta)

    def chunk_status(self, user: str, upload_id: str) -> dict[str, Any]:
        _folder, meta = self._load_chunk_meta(user, upload_id)
        return self._public_chunk_status(meta)

    def chunk_write_plan(self, user: str, upload_id: str, file_index: int, index: int) -> dict[str, Any]:
        folder, meta = self._load_chunk_meta(user, upload_id)
        spec = self._chunk_spec(meta, file_index, index)
        return {
            "path": self._piece_path(folder, file_index, index),
            "expected": spec["expected"],
        }

    def mark_chunk(self, user: str, upload_id: str, file_index: int, index: int) -> dict[str, Any]:
        with self._lock_for(upload_id):
            folder, meta = self._load_chunk_meta(user, upload_id)
            spec = self._chunk_spec(meta, file_index, index)
            part = self._piece_path(folder, file_index, index)
            if not part.is_file() or part.stat().st_size != spec["expected"]:
                raise ChunkUploadError("這一段不完整，請再試。")
            received = meta["files"][file_index]["received"]
            if index not in received:
                received.append(index)
                received.sort()
            self._write_chunk_meta(folder, meta)
            return self._public_chunk_status(meta)

    def finish_chunk_upload(self, user: str, upload_id: str) -> str:
        with self._lock_for(upload_id):
            folder, meta = self._load_chunk_meta(user, upload_id)
            sources: list[tuple[str, Path]] = []
            assembled = folder / "assembled"
            assembled.mkdir(exist_ok=True)
            for file_index, spec in enumerate(meta["files"]):
                size = int(spec["size"])
                chunk_bytes = int(meta["chunk_bytes"])
                count = (size + chunk_bytes - 1) // chunk_bytes if size else 0
                if list(spec.get("received") or []) != list(range(count)):
                    raise ChunkUploadError("上載尚未完成，請再試一次。")
                dest = assembled / f"{file_index}.bin"
                with dest.open("wb") as outgoing:
                    for index in range(count):
                        part = self._piece_path(folder, file_index, index)
                        expected = min(chunk_bytes, size - index * chunk_bytes)
                        if not part.is_file() or part.stat().st_size != expected:
                            raise ChunkUploadError("上載尚未完成，請再試一次。")
                        with part.open("rb") as incoming:
                            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                if dest.stat().st_size != size:
                    raise ChunkUploadError("上載尚未完成，請再試一次。")
                os.chmod(dest, 0o600)
                sources.append((str(spec["name"]), dest))
            sub_id = self.create_submission_from_paths(
                user=user,
                kind=str(meta.get("kind") or ""),
                note=str(meta.get("note") or ""),
                files=sources,
                record_date_start=meta.get("record_start"),
                record_date_end=meta.get("record_end"),
                purpose=str(meta.get("purpose") or ""),
            )
            shutil.rmtree(folder, ignore_errors=True)
            return sub_id

    def _chunk_spec(self, meta: dict[str, Any], file_index: int, index: int) -> dict[str, int]:
        files = meta.get("files")
        if not isinstance(files, list) or file_index < 0 or file_index >= len(files):
            raise ChunkUploadError("找不到這個檔案。")
        spec = files[file_index]
        size = int(spec["size"])
        chunk_bytes = int(meta["chunk_bytes"])
        if chunk_bytes < 1:
            raise ChunkUploadError("這一段不屬於這個檔案。")
        count = (size + chunk_bytes - 1) // chunk_bytes
        if index < 0 or index >= count:
            raise ChunkUploadError("這一段不屬於這個檔案。")
        offset = index * chunk_bytes
        expected = min(chunk_bytes, size - offset)
        return {"offset": offset, "expected": expected}

    def _piece_path(self, folder: Path, file_index: int, index: int) -> Path:
        """One piece, one file, so parallel uploads do not share a growing part file."""
        parts = (folder / "parts").resolve()
        path = (parts / f"{file_index}-{index}.part").resolve()
        if path.parent != parts:
            raise ChunkUploadError("這一段不屬於這個檔案。")
        return path

    def _chunk_dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID.fullmatch(upload_id):
            raise ChunkUploadError("找不到這次上載。")
        root = (self.local_root / ".chunk-uploads").resolve()
        folder = (root / upload_id).resolve()
        if folder.parent != root:
            raise ChunkUploadError("找不到這次上載。")
        return folder

    def _load_chunk_meta(self, user: str, upload_id: str) -> tuple[Path, dict[str, Any]]:
        folder = self._chunk_dir(upload_id)
        path = folder / "meta.json"
        if not path.is_file():
            raise ChunkUploadError("找不到這次上載。")
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChunkUploadError("找不到這次上載。") from exc
        if not isinstance(meta, dict) or meta.get("user") != user:
            raise ChunkUploadError("找不到這次上載。")
        try:
            created = float(meta.get("created_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ChunkUploadError("找不到這次上載。") from exc
        if time.time() - created > CHUNK_TTL_SECONDS:
            shutil.rmtree(folder, ignore_errors=True)
            raise ChunkUploadError("這次上載已過期，請重新選擇檔案。")
        return folder, meta

    def _write_chunk_meta(self, folder: Path, meta: dict[str, Any]) -> None:
        self._replace_private(
            folder / "meta.json",
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )

    def _lock_for(self, upload_id: str) -> threading.Lock:
        with self._chunk_guard:
            lock = self._chunk_locks.get(upload_id)
            if lock is None:
                lock = threading.Lock()
                self._chunk_locks[upload_id] = lock
            return lock

    def _public_chunk_status(self, meta: dict[str, Any]) -> dict[str, Any]:
        chunk_bytes = int(meta["chunk_bytes"])
        files = []
        for spec in meta["files"]:
            size = int(spec["size"])
            count = (size + chunk_bytes - 1) // chunk_bytes if size else 0
            files.append(
                {
                    "name": spec["name"],
                    "size": size,
                    "chunks": count,
                    "received": list(spec.get("received") or []),
                }
            )
        return {"id": meta["id"], "chunk_bytes": chunk_bytes, "files": files}

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

    def soft_delete_submission(self, sub_id: str, actor: str) -> dict[str, Any] | None:
        """Mark a submission 已刪除. Uploaded files stay where they are."""
        if not can_manage(actor):
            raise PermissionError("沒有權限。")
        if not SAFE_ID.fullmatch(sub_id):
            return None
        raw = self._refresh_from_hub(sub_id) or self._load_local_manifest(sub_id)
        if not isinstance(raw, dict):
            return None
        if manifest_deleted(raw):
            return self._present(raw)
        updated = dict(raw)
        updated["status"] = "deleted"
        updated["status_label"] = "已刪除"
        updated["deleted_by"] = actor
        updated["deleted_at"] = datetime.now(HK).isoformat()
        payload = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
        # Publish the manifest first. A failed upload leaves the local copy unchanged.
        if self.is_ready():
            self.ensure_repo()
            self._api_client().upload_file(
                path_or_fileobj=payload,
                path_in_repo=f"inbox/{sub_id}/manifest.json",
                repo_id=self.repo_id,
                repo_type="dataset",
                commit_message=f"Mark submission {sub_id} deleted",
            )
        local = self._local_dir(sub_id)
        (local / "manifest.json").write_bytes(payload)
        return self._present(updated)

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
        items = [self._present(v) for v in found.values() if isinstance(v, dict)]
        items = [item for item in items if not item.get("deleted")]
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
        deleted = manifest_deleted(out)
        labels = {
            "pending": "待處理",
            "processing": "處理中",
            "done": "已完成",
            "need_info": "待補資料",
            "deleted": "已刪除",
        }
        if deleted:
            status = "deleted"
            out["status_label"] = "已刪除"
        else:
            out["status_label"] = out.get("status_label") or labels.get(status, status)
        out["status"] = status
        out["deleted"] = deleted
        deleted_by = out.get("deleted_by") if isinstance(out.get("deleted_by"), str) else ""
        deleted_at = out.get("deleted_at") if isinstance(out.get("deleted_at"), str) else ""
        out["deleted_by"] = deleted_by.strip() if deleted else ""
        out["deleted_at"] = deleted_at.strip() if deleted else ""
        out["deleted_display"] = self._format_time(out["deleted_at"]) if deleted and out["deleted_at"] else ""
        out["files"] = [Path(str(n)).name for n in (out.get("files") or []) if n]
        out["result_files"] = [Path(str(n)).name for n in (out.get("result_files") or []) if n]
        raw_kind = out.get("kind")
        if isinstance(raw_kind, str) and raw_kind.strip():
            out["kind"] = raw_kind.strip()[:40]
        else:
            out["kind"] = KIND_PENDING
        out["note"] = out.get("note") or ""
        out["admin_note"] = out.get("admin_note") or ""
        submitted = out.get("submitted_at") or ""
        out["submitted_display"] = self._format_time(submitted)
        out["record_date_start"] = record_date_text(out.get("record_date_start"))
        out["record_date_end"] = record_date_text(out.get("record_date_end"))
        out["record_date_label"] = record_date_label(
            out["record_date_start"], out["record_date_end"]
        )
        out["purpose"] = purpose_of(out.get("purpose"))
        out["purpose_label"] = PURPOSE_LABELS[out["purpose"]]
        return out

    def _format_time(self, value: str) -> str:
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(value)
            return dt.astimezone(HK).strftime("%Y-%m-%d %H:%M") + " HKT"
        except Exception:
            return value

    def get_roster_document(self) -> dict[str, Any]:
        """Roster for the signed-in page. A missing customers/ folder is an empty roster."""
        local = self._read_local_roster()
        if not self.is_ready():
            return local
        remote = self._download_roster()
        if remote is None:
            return local
        return merge_rosters(local, remote)

    def save_roster_document(self, document: dict[str, Any], message: str) -> None:
        """Publish to the dataset first. A failed upload leaves the local roster unchanged."""
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        workbook = roster_xlsx_bytes(document)
        if self.is_ready():
            self.ensure_repo()
            self._api_client().create_commit(
                repo_id=self.repo_id,
                repo_type="dataset",
                operations=[
                    CommitOperationAdd(path_in_repo=ROSTER_JSON_PATH, path_or_fileobj=payload),
                    CommitOperationAdd(path_in_repo=ROSTER_XLSX_PATH, path_or_fileobj=workbook),
                ],
                commit_message=message,
            )
        self._write_local_roster(payload, workbook)

    def confirm_customer(self, actor: str, customer_id: str, phone_choice: str) -> dict[str, Any]:
        updated = apply_confirm(self.get_roster_document(), customer_id, actor, phone_choice)
        self.save_roster_document(updated, f"Confirm roster customer {customer_id}")
        return updated

    def edit_customer(
        self, actor: str, customer_id: str, name: str, phone: str, note: str
    ) -> dict[str, Any]:
        updated = apply_edit(self.get_roster_document(), customer_id, actor, name, phone, note)
        self.save_roster_document(updated, f"Correct roster customer {customer_id}")
        return updated

    def delete_customer(self, actor: str, customer_id: str) -> dict[str, Any]:
        updated = apply_delete(self.get_roster_document(), customer_id, actor)
        self.save_roster_document(updated, f"Delete roster customer {customer_id}")
        return updated

    def delete_customers(self, actor: str, customer_ids: list[str]) -> dict[str, Any]:
        """Soft-delete several customers in one publish. Files are not removed."""
        document = self.get_roster_document()
        updated = document
        removed = 0
        for customer_id in customer_ids:
            try:
                updated = apply_delete(updated, customer_id, actor)
            except RosterNotFound:
                continue
            removed += 1
        if removed == 0:
            raise RosterNotFound("找不到要刪除的顧客。")
        self.save_roster_document(updated, f"Delete {removed} roster customers")
        return updated

    def roster_workbook(self) -> bytes:
        return roster_xlsx_bytes(self.get_roster_document())

    def get_catalog_document(self) -> dict[str, Any]:
        """Catalog for the signed-in page. A missing catalog/ folder is an empty catalog."""
        local = self._read_local_catalog()
        if not self.is_ready():
            return local
        remote = self._download_catalog()
        if remote is None:
            return local
        return merge_catalogs(local, remote)

    def save_catalog_document(self, document: dict[str, Any], message: str) -> None:
        """Publish to the dataset first. A failed upload leaves the local catalog unchanged."""
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        workbook = catalog_xlsx_bytes(document, self.catalog_image_bytes(document))
        if self.is_ready():
            self.ensure_repo()
            self._api_client().create_commit(
                repo_id=self.repo_id,
                repo_type="dataset",
                operations=[
                    CommitOperationAdd(path_in_repo=CATALOG_JSON_PATH, path_or_fileobj=payload),
                    CommitOperationAdd(path_in_repo=CATALOG_XLSX_PATH, path_or_fileobj=workbook),
                ],
                commit_message=message,
            )
        self._write_local_catalog(payload, workbook)

    def delete_catalog_product(self, actor: str, product_id: str) -> dict[str, Any]:
        updated = apply_catalog_delete(self.get_catalog_document(), product_id, actor)
        self.save_catalog_document(updated, f"Delete catalog product {product_id}")
        return updated

    def delete_catalog_products(self, actor: str, product_ids: list[str]) -> dict[str, Any]:
        """Soft-delete several products in one publish. Uploaded files stay put."""
        document = self.get_catalog_document()
        updated = document
        removed = 0
        for product_id in product_ids:
            try:
                updated = apply_catalog_delete(updated, product_id, actor)
            except CatalogNotFound:
                continue
            removed += 1
        if removed == 0:
            raise CatalogNotFound("找不到要刪除的貨品。")
        self.save_catalog_document(updated, f"Delete {removed} catalog products")
        return updated

    def edit_catalog_product(
        self,
        actor: str,
        product_id: str,
        name: str,
        price: str,
        note: str,
        options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        updated = apply_catalog_edit(
            self.get_catalog_document(), product_id, actor, name, price, note, options
        )
        self.save_catalog_document(updated, f"Edit catalog product {product_id}")
        return updated

    def catalog_workbook(self) -> bytes:
        document = self.get_catalog_document()
        return catalog_xlsx_bytes(document, self.catalog_image_bytes(document))

    def catalog_image_bytes(self, document: dict[str, Any]) -> dict[str, bytes]:
        """Bytes already stored for catalog images. Remote image URLs are not fetched."""
        found: dict[str, bytes] = {}
        for row in document.get("products") or []:
            if not isinstance(row, dict):
                continue
            name = clean_image(row.get("image"))
            if not name or name in found or not image_is_displayable(name):
                continue
            path = self.resolve_catalog_image(row)
            if path is None:
                continue
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            if payload:
                found[name] = payload
        return found

    def resolve_catalog_image(self, product: dict[str, Any]) -> Path | None:
        """Local catalog image, or the same file name inside a source submission."""
        name = clean_image(product.get("image"))
        if not name or not image_is_displayable(name):
            return None
        local = self.local_root / "catalog" / "images" / name
        found = self._file_under_root(local)
        if found is not None:
            return found
        if self.is_ready():
            try:
                got = hf_hub_download(
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    filename=f"catalog/images/{name}",
                    token=self.token,
                )
                local.parent.mkdir(parents=True, exist_ok=True)
                self._replace_private(local, Path(got).read_bytes())
            except Exception:
                pass
            found = self._file_under_root(local)
            if found is not None:
                return found
        for sub_id in product.get("submission_ids") or []:
            if not isinstance(sub_id, str):
                continue
            path = self.local_file_path(sub_id, "files", name)
            if path is None:
                continue
            found = self._file_under_root(path)
            if found is not None and image_is_displayable(found.name):
                return found
        return None

    def _file_under_root(self, path: Path) -> Path | None:
        if not path.is_file():
            return None
        try:
            resolved = path.resolve()
            root = self.local_root.resolve()
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            return None
        return resolved

    def _roster_json_path(self) -> Path:
        return self.local_root / "customers" / "roster.json"

    def _read_local_roster(self) -> dict[str, Any]:
        path = self._roster_json_path()
        if not path.is_file():
            return empty_document()
        try:
            parsed = parse_roster_text(path.read_text(encoding="utf-8"))
        except OSError:
            return empty_document()
        return parsed if parsed is not None else empty_document()

    def _download_roster(self) -> dict[str, Any] | None:
        """Empty document when customers/roster.json is absent. None when the hub cannot be read."""
        try:
            downloaded = hf_hub_download(
                repo_id=self.repo_id,
                repo_type="dataset",
                filename=ROSTER_JSON_PATH,
                token=self.token,
                force_download=True,
            )
            parsed = parse_roster_text(Path(downloaded).read_text(encoding="utf-8"))
        except (EntryNotFoundError, RepositoryNotFoundError):
            return empty_document()
        except Exception:
            return None
        return parsed

    def _write_local_roster(self, payload: bytes, workbook: bytes) -> None:
        folder = self.local_root / "customers"
        folder.mkdir(parents=True, exist_ok=True)
        self._replace_private(folder / "roster.json", payload)
        self._replace_private(folder / "roster.xlsx", workbook)

    def _catalog_json_path(self) -> Path:
        return self.local_root / "catalog" / "catalog.json"

    def _read_local_catalog(self) -> dict[str, Any]:
        path = self._catalog_json_path()
        if not path.is_file():
            return empty_catalog()
        try:
            parsed = parse_catalog_text(path.read_text(encoding="utf-8"))
        except OSError:
            return empty_catalog()
        return parsed if parsed is not None else empty_catalog()

    def _download_catalog(self) -> dict[str, Any] | None:
        """Empty document when catalog/catalog.json is absent. None when the hub cannot be read."""
        try:
            downloaded = hf_hub_download(
                repo_id=self.repo_id,
                repo_type="dataset",
                filename=CATALOG_JSON_PATH,
                token=self.token,
                force_download=True,
            )
            parsed = parse_catalog_text(Path(downloaded).read_text(encoding="utf-8"))
        except (EntryNotFoundError, RepositoryNotFoundError):
            return empty_catalog()
        except Exception:
            return None
        return parsed

    def _write_local_catalog(self, payload: bytes, workbook: bytes) -> None:
        folder = self.local_root / "catalog"
        folder.mkdir(parents=True, exist_ok=True)
        self._replace_private(folder / "catalog.json", payload)
        self._replace_private(folder / "catalog.xlsx", workbook)

    def _replace_private(self, path: Path, payload: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def refresh_password_records(self) -> dict[str, dict[str, Any]]:
        remote = self._download_password_records()
        if remote is None:
            records = self._read_local_password_records()
        else:
            records = remote
            self._write_local_password_records(records)
        self._password_records = records
        return records

    def get_password_record(self, username: str) -> dict[str, Any] | None:
        if self._password_records is None:
            self.refresh_password_records()
        record = (self._password_records or {}).get(username)
        return dict(record) if isinstance(record, dict) else None

    def password_generation(self, username: str) -> int | None:
        record = self.get_password_record(username)
        if not record:
            return None
        try:
            return int(record.get("generation"))
        except (TypeError, ValueError):
            return None

    def save_password_record(self, username: str, record: dict[str, Any]) -> None:
        """Persist a hash record. Refuses plaintext password fields."""
        from auth import STAFF

        if username not in STAFF:
            raise ValueError("unknown user")
        safe = self._clean_password_record(record)
        if self._password_records is None:
            self.refresh_password_records()
        remote = self._download_password_records()
        if remote is None:
            if self.is_ready():
                raise RuntimeError("password store unavailable")
            base = dict(self._password_records or {})
        else:
            base = dict(remote)
        base[username] = safe
        raw = json.dumps({"version": 1, "users": base}, ensure_ascii=False, indent=2).encode("utf-8")
        if self.is_ready():
            self.ensure_repo()
            self._api_client().upload_file(
                path_or_fileobj=raw,
                path_in_repo=ACCOUNTS_REPO_PATH,
                repo_id=self.repo_id,
                repo_type="dataset",
                commit_message="Update staff password hashes",
            )
        self._write_local_password_bytes(raw)
        self._password_records = base

    def _accounts_file(self) -> Path:
        return self.local_root / "accounts.json"

    def _download_password_records(self) -> dict[str, dict[str, Any]] | None:
        if not self.is_ready():
            return None
        try:
            downloaded = hf_hub_download(
                repo_id=self.repo_id,
                repo_type="dataset",
                filename=ACCOUNTS_REPO_PATH,
                token=self.token,
                force_download=True,
            )
            return self._parse_password_file(Path(downloaded).read_text(encoding="utf-8"))
        except (EntryNotFoundError, RepositoryNotFoundError):
            return {}
        except Exception:
            return None

    def _read_local_password_records(self) -> dict[str, dict[str, Any]]:
        path = self._accounts_file()
        if not path.is_file():
            return {}
        try:
            return self._parse_password_file(path.read_text(encoding="utf-8"))
        except OSError:
            return {}

    def _write_local_password_records(self, records: dict[str, dict[str, Any]]) -> None:
        raw = json.dumps({"version": 1, "users": records}, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_local_password_bytes(raw)

    def _write_local_password_bytes(self, raw: bytes) -> None:
        path = self._accounts_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def _parse_password_file(self, text: str) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        from auth import STAFF

        for name, record in users.items():
            if name not in STAFF or not isinstance(record, dict):
                continue
            try:
                cleaned[name] = self._clean_password_record(record)
            except ValueError:
                continue
        return cleaned

    def _clean_password_record(self, record: dict[str, Any]) -> dict[str, Any]:
        for key in record:
            if str(key).lower() == "password":
                raise ValueError("plaintext password refused")
        if record.get("algo") != "pbkdf2_sha256":
            raise ValueError("incomplete record")
        salt = record.get("salt")
        digest = record.get("hash")
        if not isinstance(salt, str) or not isinstance(digest, str):
            raise ValueError("incomplete record")
        try:
            iterations = int(record["iterations"])
            dklen = int(record.get("dklen") or 32)
            generation = int(record["generation"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("incomplete record") from exc
        if iterations < 1 or iterations > 2_000_000 or dklen < 16 or dklen > 64 or generation < 1:
            raise ValueError("invalid record")
        safe: dict[str, Any] = {
            "algo": "pbkdf2_sha256",
            "iterations": iterations,
            "dklen": dklen,
            "salt": salt,
            "hash": digest,
            "generation": generation,
        }
        updated = record.get("updated_at")
        if isinstance(updated, str) and updated and len(updated) <= 64:
            safe["updated_at"] = updated
        return safe

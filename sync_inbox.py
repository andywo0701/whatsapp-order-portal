#!/usr/bin/env python3
"""Download new portal submissions into the local WhatsApp orders folder."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download, upload_file
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

from roster import (
    ROSTER_JSON_PATH,
    ROSTER_XLSX_PATH,
    empty_document,
    merge_rosters,
    parse_roster_text,
    roster_xlsx_bytes,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
DATASET_ID = os.environ.get("INBOX_DATASET", "Andyfatwo/whatsapp-order-inbox")
DEST = Path(os.environ.get("INBOX_DEST") or "/workspace/company-os/20-projects/whatsapp-orders/inbox")


def token() -> str:
    value = os.environ.get("HF_WRITE_TOKEN") or os.environ.get("HF_TOKEN") or ""
    if not value:
        sys.stderr.write("Missing HF_WRITE_TOKEN.\n")
        sys.exit(1)
    return value


def main() -> None:
    tok = token()
    api = HfApi(token=tok)
    DEST.mkdir(parents=True, exist_ok=True)
    tree = api.list_repo_tree(DATASET_ID, repo_type="dataset", path_in_repo="inbox", recursive=True)
    ids: set[str] = set()
    for entry in tree:
        path = getattr(entry, "path", "") or ""
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "inbox" and SAFE_ID.fullmatch(parts[1]):
            ids.add(parts[1])
    new_ids: list[str] = []
    for sub_id in sorted(ids):
        local = DEST / sub_id
        manifest_path = local / "manifest.json"
        already = manifest_path.exists()
        local.mkdir(parents=True, exist_ok=True)
        (local / "files").mkdir(exist_ok=True)
        (local / "results").mkdir(exist_ok=True)
        for rel in ["manifest.json"]:
            got = hf_hub_download(
                repo_id=DATASET_ID, repo_type="dataset", filename=f"inbox/{sub_id}/{rel}", token=tok
            )
            Path(got).read_bytes()
            (local / rel).write_bytes(Path(got).read_bytes())
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name in data.get("files") or []:
            name = Path(str(name)).name
            dest = local / "files" / name
            if dest.exists():
                continue
            got = hf_hub_download(
                repo_id=DATASET_ID,
                repo_type="dataset",
                filename=f"inbox/{sub_id}/files/{name}",
                token=tok,
            )
            dest.write_bytes(Path(got).read_bytes())
        if not already and data.get("status") == "pending":
            new_ids.append(sub_id)
    summary = DEST / "sync-summary.json"
    summary.write_text(json.dumps({"new": new_ids, "all": sorted(ids)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"new": new_ids, "count_all": len(ids)}, ensure_ascii=False))


def publish_result(sub_id: str, files: list[Path], status: str, admin_note: str = "") -> None:
    if not SAFE_ID.fullmatch(sub_id):
        raise ValueError("invalid id")
    tok = token()
    dest_dir = DEST / sub_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dest_dir / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"id": sub_id, "files": []}
    labels = {"pending": "待處理", "processing": "處理中", "done": "已完成", "need_info": "待補資料"}
    result_names = []
    results_dir = dest_dir / "results"
    results_dir.mkdir(exist_ok=True)
    for path in files:
        target = results_dir / path.name
        if path.resolve() != target.resolve():
            target.write_bytes(path.read_bytes())
        upload_file(
            path_or_fileobj=target.read_bytes(),
            path_in_repo=f"inbox/{sub_id}/results/{target.name}",
            repo_id=DATASET_ID,
            repo_type="dataset",
            token=tok,
            commit_message=f"Add result for {sub_id}",
        )
        result_names.append(target.name)
    data["status"] = status
    data["status_label"] = labels.get(status, status)
    data["result_files"] = result_names
    if admin_note:
        data["admin_note"] = admin_note
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upload_file(
        path_or_fileobj=manifest_path.read_bytes(),
        path_in_repo=f"inbox/{sub_id}/manifest.json",
        repo_id=DATASET_ID,
        repo_type="dataset",
        token=tok,
        commit_message=f"Update status for {sub_id}",
    )


_LOOSE_PENDING = {"need_info", "待補資料"}


def _loosen_roster(data: dict) -> dict:
    """Map the scanner's looser status words onto the roster schema."""
    customers = data.get("customers")
    if not isinstance(customers, list):
        return data
    for row in customers:
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if isinstance(status, str) and status.strip() in _LOOSE_PENDING:
            row["status"] = "pending"
        label = row.get("狀態")
        if isinstance(label, str) and label.strip() in _LOOSE_PENDING:
            row["狀態"] = "待確認"
    return data


def prepare_roster_payload(data: object) -> dict:
    """Check a scanner roster and accept its slightly looser status values."""
    if not isinstance(data, dict) or not isinstance(data.get("customers"), list):
        raise ValueError("roster json must be an object with a customers list")
    for row in data["customers"]:
        if not isinstance(row, dict):
            raise ValueError("each customer must be an object")
        for key in ("name", "phone"):
            if key in row and row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"{key} must be a string")
    return _loosen_roster(data)


def load_official_roster(tok: str) -> dict:
    """The roster already published for the website. Missing means empty.

    Any other download failure is raised, so a publish cannot replace the
    official files with only the scanner's new rows.
    """
    try:
        got = hf_hub_download(
            repo_id=DATASET_ID,
            repo_type="dataset",
            filename=ROSTER_JSON_PATH,
            token=tok,
        )
    except (EntryNotFoundError, RepositoryNotFoundError):
        return empty_document()
    parsed = parse_roster_text(Path(got).read_text(encoding="utf-8"))
    if parsed is None:
        raise ValueError("official customers/roster.json is not usable JSON")
    return parsed


def publish_roster(roster_json: Path) -> None:
    """Merge into the official roster and publish both website files.

    The 顧客名冊 page reads only ``customers/roster.json`` (and the download
    reads ``customers/roster.xlsx``). Putting 顧客名冊.xlsx under
    ``inbox/{id}/results/`` does not update that list. Call this whenever the
    scanner changes the customer roster.

    The upload is one dataset commit of both files. If that commit fails, the
    previous official roster is left unchanged. A blank phone does not erase a
    known number. The same name with a different phone stays 待確認. Rows
    already deleted stay deleted. ``need_info`` and ``待補資料`` are stored as
    ``pending``. A missing id is assigned (``c`` plus hex).
    """
    path = Path(roster_json)
    incoming = prepare_roster_payload(json.loads(path.read_text(encoding="utf-8")))
    tok = token()
    current = load_official_roster(tok)
    merged = merge_rosters(current, incoming)
    payload = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
    workbook = roster_xlsx_bytes(merged)
    api = HfApi(token=tok)
    api.create_commit(
        repo_id=DATASET_ID,
        repo_type="dataset",
        operations=[
            CommitOperationAdd(path_in_repo=ROSTER_JSON_PATH, path_or_fileobj=payload),
            CommitOperationAdd(path_in_repo=ROSTER_XLSX_PATH, path_or_fileobj=workbook),
        ],
        commit_message="Publish customer roster",
    )
    print("Published customers/roster.json and customers/roster.xlsx")


def validate_catalog_payload(data: object) -> None:
    """Refuse a catalog publish that would make the portal invent a price."""
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("catalog json must be an object with a products list")
    for row in data["products"]:
        if not isinstance(row, dict):
            raise ValueError("each product must be an object")
        for key in ("name", "price", "image", "note"):
            if key in row and row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"{key} must be a string")
        for key in ("options", "variants", "specs", "sizes", "colors", "規格", "尺碼", "顏色"):
            if key not in row or row[key] is None:
                continue
            collection = row[key]
            if isinstance(collection, str):
                continue
            if isinstance(collection, dict):
                collection = [collection]
            if not isinstance(collection, list):
                raise ValueError(f"{key} must be a list")
            for option in collection:
                if isinstance(option, str):
                    continue
                if not isinstance(option, dict):
                    raise ValueError("each option must be an object or string")
                for field in ("price", "value", "size", "color", "other", "label", "價錢", "尺碼", "顏色", "單價"):
                    if field in option and option[field] is not None and not isinstance(option[field], str):
                        raise ValueError(f"option {field} must be a string")


def publish_catalog(catalog_json: Path) -> None:
    """Upload catalog/catalog.json. The portal merges it and does not OCR prices.

    name and price must be strings when present, so a price keeps the characters
    the scanner actually saw. Option prices must be strings too. A blank price
    does not erase a price already on the portal, omitting options does not
    erase known option prices, and a deleted product is not restored.
    """
    path = Path(catalog_json)
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_catalog_payload(data)
    tok = token()
    upload_file(
        path_or_fileobj=path.read_bytes(),
        path_in_repo="catalog/catalog.json",
        repo_id=DATASET_ID,
        repo_type="dataset",
        token=tok,
        commit_message="Publish product catalog",
    )
    print("Published catalog/catalog.json")


if __name__ == "__main__":
    main()

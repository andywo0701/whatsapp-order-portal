#!/usr/bin/env python3
"""Download new portal submissions into the local WhatsApp orders folder."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download, upload_file

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


if __name__ == "__main__":
    main()

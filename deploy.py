#!/usr/bin/env python3
"""Create or update the protected Hugging Face Space. Never print secrets."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from huggingface_hub import HfApi

SPACE_ID = os.environ.get("SPACE_ID", "Andyfatwo/whatsapp-order-portal")
DATASET_ID = os.environ.get("INBOX_DATASET", "Andyfatwo/whatsapp-order-inbox")
ROOT = Path(__file__).resolve().parent
SKIP = {".env", ".venv", "__pycache__", "local_data", ".git"}


def token() -> str:
    value = os.environ.get("HF_WRITE_TOKEN") or os.environ.get("HF_TOKEN") or ""
    if not value:
        sys.stderr.write("Missing HF_WRITE_TOKEN.\n")
        sys.exit(1)
    return value


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP or part.endswith(".pyc") for part in rel.parts):
            continue
        files.append(path)
    return files


def main() -> None:
    api = HfApi(token=token())
    api.create_repo(
        SPACE_ID,
        repo_type="space",
        space_sdk="docker",
        private=False,
        exist_ok=True,
    )
    # Free accounts cannot use protected visibility (hides source). Public Space
    # plus app login is fine: secrets stay in Space secrets, uploads go to a private dataset.
    try:
        api.update_repo_settings(SPACE_ID, repo_type="space", private=False)
    except Exception as exc:
        sys.stderr.write(f"Could not confirm public visibility ({exc}). Leaving current visibility.\n")

    api.create_repo(DATASET_ID, repo_type="dataset", private=True, exist_ok=True)

    existing_secrets = {}
    try:
        existing_secrets = {item["name"]: item for item in (api.get_space_secrets(SPACE_ID) or [])}
    except Exception:
        existing_secrets = {}

    to_set: dict[str, str] = {}
    if os.environ.get("SITE_USERNAME"):
        to_set["SITE_USERNAME"] = os.environ["SITE_USERNAME"]
    if os.environ.get("SITE_PASSWORD"):
        to_set["SITE_PASSWORD"] = os.environ["SITE_PASSWORD"]
    if os.environ.get("SITE_USERS"):
        to_set["SITE_USERS"] = os.environ["SITE_USERS"]
    to_set["HF_TOKEN"] = token()
    to_set["INBOX_DATASET"] = DATASET_ID
    to_set["HTTPS_ONLY"] = "true"
    if "SESSION_SECRET" not in existing_secrets:
        to_set["SESSION_SECRET"] = secrets.token_hex(32)

    for key, value in to_set.items():
        api.add_space_secret(SPACE_ID, key, value)

    api.upload_folder(
        folder_path=str(ROOT),
        repo_id=SPACE_ID,
        repo_type="space",
        allow_patterns=None,
        ignore_patterns=[".env", ".venv/**", "**/__pycache__/**", "local_data/**", ".git/**", "*.pyc"],
        commit_message="Deploy order upload portal",
    )
    print(f"Space: https://huggingface.co/spaces/{SPACE_ID}")
    print(f"Dataset: https://huggingface.co/datasets/{DATASET_ID} (private)")


if __name__ == "__main__":
    main()

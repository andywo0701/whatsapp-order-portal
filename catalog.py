"""Product catalog records. The portal never invents a name or a price."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auth import STAFF
from roster import xlsx_bytes

HK = ZoneInfo("Asia/Hong_Kong")

CATALOG_JSON_PATH = "catalog/catalog.json"
CATALOG_XLSX_PATH = "catalog/catalog.xlsx"
CATALOG_DELETE_CONFIRM = "確定刪除此貨品？"

XLSX_HEADERS = ("貨品名稱", "圖片", "價錢", "備註", "最後更新日期", "來源提交")

PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SUBMISSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9._\u4e00-\u9fff\-()+ ]+$")
WEB_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

NAME_LIMIT = 80
PRICE_LIMIT = 60
NOTE_LIMIT = 500
PRODUCT_LIMIT = 5000

BLANK_PRICE_WORDS = {"尚缺", "—", "-", "無", "没有", "沒有", "n/a", "na", "none"}


class CatalogError(ValueError):
    """A catalog change the page can show to staff."""


class CatalogNotFound(CatalogError):
    """No catalog row has this id."""


def empty_document() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "updated_by": "",
        "products": [],
        "deleted": [],
    }


def parse_catalog_text(text: str) -> dict[str, Any] | None:
    """Return a catalog document, or None when the file is not usable JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return normalize_document(data)


def normalize_document(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        return empty_document()
    products: list[dict[str, Any]] = []
    by_name: dict[str, int] = {}
    used: set[str] = set()
    raw_rows = data.get("products")
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            row = normalize_product(raw, used)
            if row is None:
                continue
            previous = by_name.get(row["name"])
            if previous is None:
                by_name[row["name"]] = len(products)
                used.add(row["id"])
                products.append(row)
            else:
                products[previous] = _merge_pair(products[previous], row)
            if len(products) >= PRODUCT_LIMIT:
                break
    return {
        "version": 1,
        "updated_at": _short_text(data.get("updated_at"), 40),
        "updated_by": _actor_name(data.get("updated_by")),
        "products": products,
        "deleted": _normalize_deleted(data.get("deleted")),
    }


def normalize_product(raw: object, used: set[str] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean_name(_first(raw, ("name", "貨品名稱", "產品", "商品")))
    if not name:
        return None
    taken = set(used or ())
    return {
        "id": _product_id(raw.get("id"), name, taken),
        "name": name,
        "image": clean_image(_first(raw, ("image", "圖片", "image_filename"))),
        "price": clean_price(_first(raw, ("price", "價錢", "價格"))),
        "note": _clean_note(_first(raw, ("note", "備註", "notes"))),
        "updated_at": _short_text(_first(raw, ("updated_at", "最後更新日期")), 40),
        "submission_ids": _submission_ids(raw.get("submission_ids") or raw.get("submissions")),
    }


def clean_price(value: object) -> str:
    """Keep the original price text. Numbers are ignored, not reformatted."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r", "").replace("\n", "").strip()
    if not text or text.casefold() in BLANK_PRICE_WORDS:
        return ""
    return text[:PRICE_LIMIT]


def clean_image(value: object) -> str:
    """A file name only. URLs and other paths are not fetched."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "://" in text or text.startswith("//"):
        return ""
    name = Path(text.replace("\\", "/")).name
    if not name or name in {".", ".."} or len(name) > 120:
        return ""
    if not IMAGE_RE.fullmatch(name):
        return ""
    return name


def image_is_displayable(name: str) -> bool:
    return Path(name).suffix.lower() in WEB_IMAGE_EXT


def merge_catalogs(local: object, published: object) -> dict[str, Any]:
    """Merge a bot publish into the catalog already on the portal.

    A blank published price never erases a known price, and a non-string price
    is ignored. Rows already deleted by Super Admin are not restored.
    Omitting a product from the publish does not delete it.
    """
    base = normalize_document(local)
    incoming = normalize_document(published) if published is not None else empty_document()
    deleted = _union_deleted(base["deleted"], incoming["deleted"])
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    by_name: dict[str, str] = {}
    for row in base["products"]:
        if _blocked(row, deleted):
            continue
        rows[row["id"]] = copy.deepcopy(row)
        order.append(row["id"])
        if row["name"] not in by_name:
            by_name[row["name"]] = row["id"]
    for row in incoming["products"]:
        if _blocked(row, deleted):
            continue
        match_id = row["id"] if row["id"] in rows else by_name.get(row["name"])
        if match_id is None:
            fresh = copy.deepcopy(row)
            while fresh["id"] in rows:
                fresh["id"] = _stable_id(fresh["name"] + fresh["id"], set(rows))
            if _blocked(fresh, deleted):
                continue
            rows[fresh["id"]] = fresh
            order.append(fresh["id"])
            by_name[fresh["name"]] = fresh["id"]
            continue
        merged = _merge_pair(rows[match_id], row)
        if merged["name"] != rows[match_id]["name"]:
            by_name.pop(rows[match_id]["name"], None)
        rows[match_id] = merged
        by_name[merged["name"]] = match_id
    return normalize_document(
        {
            "version": 1,
            "updated_at": incoming["updated_at"] or base["updated_at"],
            "updated_by": incoming["updated_by"] or base["updated_by"],
            "products": [rows[row_id] for row_id in order if row_id in rows],
            "deleted": deleted,
        }
    )


def apply_delete(document: dict[str, Any], product_id: str, actor: str) -> dict[str, Any]:
    _require_super(actor)
    doc = normalize_document(copy.deepcopy(document))
    row = _find(doc, product_id)
    now = _now()
    tomb = {
        "id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "deleted_by": actor,
        "deleted_at": now,
    }
    doc["products"] = [item for item in doc["products"] if item["id"] != row["id"]]
    doc["deleted"] = _union_deleted(doc["deleted"], [tomb])
    doc["updated_at"] = now
    doc["updated_by"] = actor
    return normalize_document(doc)


def filter_products(products: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip() if isinstance(query, str) else ""
    if not needle:
        return list(products)
    folded = needle.casefold()
    found: list[dict[str, Any]] = []
    for row in products:
        name = str(row.get("name") or "")
        price = str(row.get("price") or "")
        note = str(row.get("note") or "")
        if folded in name.casefold() or folded in price.casefold() or folded in note.casefold():
            found.append(row)
    return found


def present_product(row: dict[str, Any]) -> dict[str, Any]:
    price = str(row.get("price") or "")
    image = str(row.get("image") or "")
    note = str(row.get("note") or "")
    updated = str(row.get("updated_at") or "")
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "image": image,
        "price": price,
        "price_display": price or "尚缺",
        "price_missing": not price,
        "note": note,
        "updated_at": updated,
        "updated_display": _format_time(updated),
        "submission_ids": list(row.get("submission_ids") or []),
    }


def catalog_xlsx_bytes(document: dict[str, Any]) -> bytes:
    rows = [present_product(row) for row in normalize_document(document)["products"]]
    sheet_rows = [list(XLSX_HEADERS)]
    for row in rows:
        sheet_rows.append(
            [
                row["name"],
                row["image"] or "沒有",
                row["price_display"],
                row["note"],
                row["updated_display"],
                "、".join(row["submission_ids"]),
            ]
        )
    return xlsx_bytes(sheet_rows, "貨品清單")


def _merge_pair(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    row = copy.deepcopy(base)
    if incoming.get("name"):
        row["name"] = incoming["name"]
    if incoming.get("price"):
        row["price"] = incoming["price"]
    if incoming.get("image"):
        row["image"] = incoming["image"]
    if incoming.get("note"):
        row["note"] = incoming["note"]
    if incoming.get("updated_at"):
        row["updated_at"] = incoming["updated_at"]
    row["submission_ids"] = _limit_ids(list(base.get("submission_ids") or []) + list(incoming.get("submission_ids") or []))
    return row


def _blocked(row: dict[str, Any], deleted: list[dict[str, Any]]) -> bool:
    for tomb in deleted:
        if row.get("id") and row["id"] == tomb.get("id"):
            return True
        if row.get("name") and row["name"] == tomb.get("name"):
            return True
    return False


def _find(document: dict[str, Any], product_id: str) -> dict[str, Any]:
    if not isinstance(product_id, str) or not PRODUCT_ID_RE.fullmatch(product_id):
        raise CatalogNotFound("找不到這項貨品。")
    for row in document["products"]:
        if row["id"] == product_id:
            return row
    raise CatalogNotFound("找不到這項貨品。")


def _require_super(actor: str) -> None:
    if STAFF.get(actor) != "super_admin":
        raise PermissionError("沒有權限。")


def _product_id(explicit: object, name: str, used: set[str]) -> str:
    if isinstance(explicit, str) and PRODUCT_ID_RE.fullmatch(explicit.strip()) and explicit.strip() not in used:
        return explicit.strip()
    return _stable_id(name, used)


def _stable_id(name: str, used: set[str]) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    candidate = "p" + digest[:8]
    if candidate not in used:
        return candidate
    for count in range(2, 50):
        extra = "p" + digest[:6] + f"{count:02d}"
        if extra not in used:
            return extra
    fallback = "p" + digest[:16]
    return fallback if fallback not in used else "p" + digest[:20]


def _union_deleted(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(left) + list(right):
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "") + "|" + str(item.get("name") or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[-500:]


def _normalize_deleted(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    deleted = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        product_id = item.get("id")
        if isinstance(product_id, str) and PRODUCT_ID_RE.fullmatch(product_id.strip()):
            product_id = product_id.strip()
        else:
            product_id = ""
        deleted.append(
            {
                "id": product_id,
                "name": _clean_name(item.get("name")),
                "price": clean_price(item.get("price")),
                "deleted_by": _actor_name(item.get("deleted_by")),
                "deleted_at": _short_text(item.get("deleted_at"), 40),
            }
        )
    return deleted[-500:]


def _submission_ids(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return _limit_ids([item for item in raw if isinstance(item, str)])


def _limit_ids(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        text = value.strip()
        if SUBMISSION_ID_RE.fullmatch(text) and text not in found:
            found.append(text)
        if len(found) >= 20:
            break
    return found


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in raw and raw.get(key) not in (None, ""):
            return raw.get(key)
    return ""


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return text[:NAME_LIMIT]


def _clean_note(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:NOTE_LIMIT]


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _actor_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text in STAFF or text == "scanner":
        return text
    if text and len(text) <= 40 and "\n" not in text:
        return text
    return ""


def _now() -> str:
    return datetime.now(HK).replace(microsecond=0).isoformat()


def _format_time(value: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HK)
    return parsed.astimezone(HK).strftime("%Y-%m-%d %H:%M") + " HKT"

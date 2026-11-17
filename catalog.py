"""Product catalog records. The portal never invents a name or a price."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from auth import STAFF, can_manage

HK = ZoneInfo("Asia/Hong_Kong")

CATALOG_JSON_PATH = "catalog/catalog.json"
CATALOG_XLSX_PATH = "catalog/catalog.xlsx"
CATALOG_DELETE_CONFIRM = "確定刪除此貨品？"
CATALOG_EDIT_CONFIRM = "確定儲存此貨品？"
SOURCE_MANUAL = "manual"
SOURCE_SCANNER = "scanner"

XLSX_HEADERS = (
    "貨品名稱",
    "圖片",
    "尺碼／規格",
    "顏色",
    "其他規格",
    "價錢",
    "備註",
    "最後更新日期",
    "來源提交",
)

PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SUBMISSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9._\u4e00-\u9fff\-()+ ]+$")
WEB_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

NAME_LIMIT = 80
PRICE_LIMIT = 60
NOTE_LIMIT = 500
SPEC_LIMIT = 40
OTHER_LIMIT = 80
PRODUCT_LIMIT = 5000
OPTION_LIMIT = 80
EDIT_SLOT_LIMIT = 80

BLANK_PRICE_WORDS = {"尚缺", "—", "-", "無", "没有", "沒有", "n/a", "na", "none"}
OPTION_LIST_KEYS = ("options", "variants", "specs", "規格", "規格選項")
SIZE_LIST_KEYS = ("sizes", "尺碼", "尺碼／規格", "尺碼/規格")
COLOR_LIST_KEYS = ("colors", "colours", "顏色")
OTHER_LIST_KEYS = ("others", "其他", "其他規格")
_KIND_SIZE = {"size", "sizes", "尺碼", "規格", "尺碼／規格", "尺碼/規格"}
_KIND_COLOR = {"color", "colour", "colors", "colours", "顏色"}
_KIND_OTHER = {"other", "others", "其他", "其他規格"}


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
    return _finish_document(_normalize_document(data))


def _normalize_document(data: object) -> dict[str, Any]:
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


def _finish_document(document: dict[str, Any]) -> dict[str, Any]:
    for row in document.get("products") or []:
        if isinstance(row, dict):
            row.pop("options_stated", None)
            if not isinstance(row.get("options"), list):
                row["options"] = []
    return document


def normalize_product(raw: object, used: set[str] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean_name(_first(raw, ("name", "貨品名稱", "產品", "商品")))
    if not name:
        return None
    taken = set(used or ())
    options, stated = _options_from(raw)
    return {
        "id": _product_id(raw.get("id"), name, taken),
        "name": name,
        "image": clean_image(_first(raw, ("image", "圖片", "image_filename"))),
        "price": clean_price(_first(raw, ("price", "價錢", "價格"))),
        "note": _clean_note(_first(raw, ("note", "備註", "notes"))),
        "source": canonical_source(_first_present(raw, ("source", "來源"))),
        "options": options,
        "options_stated": stated,
        "updated_at": _short_text(_first(raw, ("updated_at", "最後更新日期")), 40),
        "submission_ids": _submission_ids(raw.get("submission_ids") or raw.get("submissions")),
    }


def canonical_source(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text in {SOURCE_MANUAL, "人手輸入"}:
        return SOURCE_MANUAL
    if text in {SOURCE_SCANNER, "掃描", "掃描發布", "scanner"}:
        return SOURCE_SCANNER
    return ""


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
    is ignored. The same rule applies to option prices. Omitting options, or
    leaving an option price blank, does not wipe a known option price. A row
    marked 人手輸入 keeps that source, so a later blank cell cannot clear the
    hand edit. Rows already deleted by staff are not restored. Omitting
    a product from the publish does not delete it.
    """
    base = _normalize_document(local)
    incoming = _normalize_document(published) if published is not None else empty_document()
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


def apply_edit(
    document: dict[str, Any],
    product_id: str,
    actor: str,
    name: str,
    price: str,
    note: str,
    options: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Staff edit. A blank price does not erase a known price or option price.

    Passing options replaces the saved option list. A blank option price keeps
    the price already stored for that slot. Checking clear removes that option.
    The product id stays the same, and source becomes 人手輸入.
    """
    _require_staff(actor)
    cleaned_name = _clean_name(name)
    if not cleaned_name:
        raise CatalogError("請填寫貨品名稱。")
    doc = normalize_document(copy.deepcopy(document))
    row = _find(doc, product_id)
    if _name_conflicts(doc, cleaned_name, row["id"]):
        raise CatalogError("已經有同名貨品，或這個名稱已經刪除。")
    cleaned_price = clean_price(price)
    if cleaned_price:
        row["price"] = cleaned_price
    row["name"] = cleaned_name
    row["note"] = _clean_note(note)
    if options is not None:
        row["options"] = _apply_option_edits(list(row.get("options") or []), options)
    row["source"] = SOURCE_MANUAL
    now = _now()
    row["updated_at"] = now
    doc["updated_at"] = now
    doc["updated_by"] = actor
    return normalize_document(doc)


def edit_slots(options: list[dict[str, Any]] | None, extra: int = 3, limit: int = EDIT_SLOT_LIMIT) -> list[dict[str, str]]:
    """Existing specification rows, plus a few blank rows for new ones."""
    slots: list[dict[str, str]] = []
    for option in options or []:
        if len(slots) >= limit:
            break
        if not isinstance(option, dict):
            continue
        size = _clean_spec(option.get("size"))
        color = _clean_spec(option.get("color"))
        other = _clean_spec(option.get("other"), OTHER_LIMIT)
        raw_price = option.get("price")
        slots.append(
            {
                "size": size,
                "color": color,
                "other": other,
                "price": raw_price if isinstance(raw_price, str) else "",
                "prev_size": size,
                "prev_color": color,
                "prev_other": other,
                "clear": "",
            }
        )
    blank = {
        "size": "",
        "color": "",
        "other": "",
        "price": "",
        "prev_size": "",
        "prev_color": "",
        "prev_other": "",
        "clear": "",
    }
    if len(slots) >= limit:
        return slots
    target = min(limit, max(len(slots) + extra, extra))
    while len(slots) < target:
        slots.append(dict(blank))
    return slots


def apply_delete(document: dict[str, Any], product_id: str, actor: str) -> dict[str, Any]:
    _require_staff(actor)
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
        specs = _spec_blob(row.get("options") or [])
        if (
            folded in name.casefold()
            or folded in price.casefold()
            or folded in note.casefold()
            or folded in specs.casefold()
        ):
            found.append(row)
    return found


def present_product(row: dict[str, Any]) -> dict[str, Any]:
    price = str(row.get("price") or "")
    image = str(row.get("image") or "")
    note = str(row.get("note") or "")
    updated = str(row.get("updated_at") or "")
    source = canonical_source(row.get("source"))
    options = [_present_option(item) for item in row.get("options") or [] if isinstance(item, dict)]
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "image": image,
        "price": price,
        "price_display": price or "尚缺",
        "price_missing": not price,
        "note": note,
        "source": source,
        "source_label": _source_label(source),
        "options": options,
        "spec_summary": _spec_summary(options),
        "updated_at": updated,
        "updated_display": _format_time(updated),
        "submission_ids": list(row.get("submission_ids") or []),
    }


def catalog_xlsx_bytes(document: dict[str, Any], images: dict[str, bytes] | None = None) -> bytes:
    """Workbook for the published catalog. Images are embedded from stored bytes only."""
    stored = images or {}
    sheet_rows: list[list[str]] = [list(XLSX_HEADERS)]
    image_rows: dict[int, tuple[str, bytes, str]] = {}
    for row in normalize_document(document)["products"]:
        shown = present_product(row)
        excel_row = len(sheet_rows) + 1
        payload, extension = _stored_image(shown["image"], stored)
        sheet_rows.append(
            [
                shown["name"],
                "",
                _join_unique(item["size"] for item in shown["options"]),
                _join_unique(item["color"] for item in shown["options"]),
                _join_unique(item["other"] for item in shown["options"]),
                shown["price_display"],
                shown["note"],
                shown["updated_display"],
                "、".join(shown["submission_ids"]),
            ]
        )
        if payload and extension:
            image_rows[excel_row] = (shown["image"], payload, extension)
        for option in shown["options"]:
            if not option["price"]:
                continue
            sheet_rows.append(
                [
                    shown["name"],
                    "",
                    option["size"],
                    option["color"],
                    option["other"],
                    option["price"],
                    "",
                    "",
                    "",
                ]
            )
    return _catalog_xlsx(sheet_rows, image_rows)


def _stored_image(name: str, images: dict[str, bytes]) -> tuple[bytes, str]:
    if not name or name not in images:
        return b"", ""
    payload = images.get(name) or b""
    if not isinstance(payload, bytes) or not payload:
        return b"", ""
    extension = _image_extension(name, payload)
    if not extension:
        return b"", ""
    return payload, extension


def _image_extension(name: str, payload: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_CONTENT_TYPES and image_is_displayable(name):
        return suffix
    if payload.startswith(b"\x89PNG"):
        return ".png"
    if payload.startswith(b"\xff\xd8"):
        return ".jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    return ""


def _present_option(item: dict[str, Any]) -> dict[str, Any]:
    price = clean_price(item.get("price"))
    return {
        "size": _clean_spec(item.get("size")),
        "color": _clean_spec(item.get("color")),
        "other": _clean_spec(item.get("other"), OTHER_LIMIT),
        "price": price,
        "price_display": price or "尚缺",
        "price_missing": not price,
    }


def _source_label(source: str) -> str:
    if source == SOURCE_MANUAL:
        return "人手輸入"
    if source == SOURCE_SCANNER:
        return "掃描發布"
    return ""


def _spec_summary(options: list[dict[str, Any]]) -> str:
    parts = [
        _join_unique(item.get("size") or "" for item in options),
        _join_unique(item.get("color") or "" for item in options),
        _join_unique(item.get("other") or "" for item in options),
    ]
    return " · ".join(part for part in parts if part)


def _spec_blob(options: object) -> str:
    if not isinstance(options, list):
        return ""
    chunks: list[str] = []
    for item in options:
        if not isinstance(item, dict):
            continue
        chunks.extend(
            [
                str(item.get("size") or ""),
                str(item.get("color") or ""),
                str(item.get("other") or ""),
                str(item.get("price") or ""),
            ]
        )
    return " ".join(part for part in chunks if part)


def _join_unique(values: object) -> str:
    found: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)
    return "、".join(found)


def _merge_pair(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    row = copy.deepcopy(base)
    if incoming.get("name"):
        row["name"] = incoming["name"]
    # A blank or omitted price never replaces a price we already know.
    if incoming.get("price"):
        row["price"] = incoming["price"]
    if incoming.get("image"):
        row["image"] = incoming["image"]
    if incoming.get("note"):
        row["note"] = incoming["note"]
    if incoming.get("updated_at"):
        row["updated_at"] = incoming["updated_at"]
    row["options"] = _merge_options(
        list(base.get("options") or []),
        list(incoming.get("options") or []),
        bool(incoming.get("options_stated")),
    )
    if base.get("source") == SOURCE_MANUAL:
        row["source"] = SOURCE_MANUAL
    elif incoming.get("source"):
        row["source"] = incoming["source"]
    else:
        row["source"] = base.get("source") or ""
    row["submission_ids"] = _limit_ids(
        list(base.get("submission_ids") or []) + list(incoming.get("submission_ids") or [])
    )
    row["options_stated"] = bool(base.get("options_stated") or incoming.get("options_stated") or row["options"])
    return row


def _merge_options(base: list[dict[str, Any]], incoming: list[dict[str, Any]], stated: bool) -> list[dict[str, Any]]:
    """Union option rows. A missing list, or a blank option price, keeps the known price."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for item in base:
        key = _option_key(item)
        if key == ("", "", ""):
            continue
        merged[key] = copy.deepcopy(item)
        order.append(key)
    if not stated:
        return [merged[key] for key in order]
    for item in incoming:
        key = _option_key(item)
        if key == ("", "", ""):
            continue
        if key in merged:
            if item.get("price"):
                merged[key]["price"] = item["price"]
            continue
        merged[key] = copy.deepcopy(item)
        order.append(key)
        if len(order) >= OPTION_LIMIT:
            break
    return [merged[key] for key in order][:OPTION_LIMIT]


def _apply_option_edits(existing: list[dict[str, Any]], submitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = {_option_key(item): str(item.get("price") or "") for item in existing}
    chosen: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    kept = 0
    for item in submitted:
        if not isinstance(item, dict) or item.get("clear"):
            continue
        size = _clean_spec(item.get("size"))
        color = _clean_spec(item.get("color"))
        other = _clean_spec(item.get("other"), OTHER_LIMIT)
        if not size and not color and not other:
            # An emptied row is not a deletion. Only 清除 removes a saved option.
            size = _clean_spec(item.get("prev_size"))
            color = _clean_spec(item.get("prev_color"))
            other = _clean_spec(item.get("prev_other"), OTHER_LIMIT)
        if not size and not color and not other:
            continue
        kept += 1
        if kept > OPTION_LIMIT:
            raise CatalogError("規格太多。")
        key = (size, color, other)
        prev = (
            _clean_spec(item.get("prev_size")),
            _clean_spec(item.get("prev_color")),
            _clean_spec(item.get("prev_other"), OTHER_LIMIT),
        )
        price = clean_price(item.get("price"))
        if not price:
            if key in known and known[key]:
                price = known[key]
            elif prev in known and known[prev]:
                price = known[prev]
        if key in seen:
            for current in chosen:
                if _option_key(current) == key and price and not current.get("price"):
                    current["price"] = price
            continue
        seen.add(key)
        chosen.append({"size": size, "color": color, "other": other, "price": price})
    return chosen


def _name_conflicts(document: dict[str, Any], name: str, product_id: str) -> bool:
    for item in document["products"]:
        if item["id"] != product_id and item.get("name") == name:
            return True
    for tomb in document["deleted"]:
        if tomb.get("name") == name and tomb.get("id") != product_id:
            return True
    return False


def _options_from(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    for key in OPTION_LIST_KEYS:
        if key in raw and raw.get(key) is not None:
            return _collapse_options(_option_list(raw.get(key))), True
    collected: list[dict[str, Any]] = []
    stated = False
    for keys, field in (
        (SIZE_LIST_KEYS, "size"),
        (COLOR_LIST_KEYS, "color"),
        (OTHER_LIST_KEYS, "other"),
    ):
        for key in keys:
            if key not in raw or raw.get(key) is None:
                continue
            stated = True
            collected.extend(_dimension_list(raw.get(key), field))
            break
    if not stated:
        return [], False
    return _collapse_options(collected), True


def _option_list(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        parsed = _normalize_option(value)
        return [parsed] if parsed else []
    if not isinstance(value, list):
        return []
    found: list[dict[str, Any]] = []
    for item in value:
        parsed = _normalize_option(item)
        if parsed:
            found.append(parsed)
    return found


def _dimension_list(value: object, field: str) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    found: list[dict[str, Any]] = []
    limit = OTHER_LIMIT if field == "other" else SPEC_LIMIT
    for item in values:
        if isinstance(item, str):
            text = _clean_spec(item, limit)
            if not text:
                continue
            row = {"size": "", "color": "", "other": "", "price": ""}
            row[field] = text
            found.append(row)
            continue
        if not isinstance(item, dict):
            continue
        parsed = _normalize_option(item)
        if not parsed:
            continue
        if not parsed[field]:
            text = parsed["other"] or parsed["size"] or parsed["color"]
            parsed = dict(parsed)
            parsed["size"] = ""
            parsed["color"] = ""
            parsed["other"] = ""
            parsed[field] = _clean_spec(text, limit)
        found.append(parsed)
    return found


def _normalize_option(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = _clean_spec(raw)
        if not text:
            return None
        return {"size": text, "color": "", "other": "", "price": ""}
    if not isinstance(raw, dict):
        return None
    size = _clean_spec(_first(raw, ("size", "尺碼", "尺碼／規格", "尺碼/規格")))
    color = _clean_spec(_first(raw, ("color", "colour", "顏色")))
    other = _clean_spec(_first(raw, ("other", "其他", "其他規格")), OTHER_LIMIT)
    label = _clean_spec(_first(raw, ("label", "名稱")))
    kind = _short_text(_first_present(raw, ("kind", "type", "類型")), 40)
    value = _clean_spec(_first(raw, ("value", "選項", "option")), OTHER_LIMIT)
    price = clean_price(_first(raw, ("price", "價錢", "價格", "單價")))
    folded = kind.casefold()
    if value and folded in {item.casefold() for item in _KIND_SIZE} and not size:
        size = _clean_spec(value)
    elif value and folded in {item.casefold() for item in _KIND_COLOR} and not color:
        color = _clean_spec(value)
    elif value and (not kind or folded in {item.casefold() for item in _KIND_OTHER}) and not other and not size and not color:
        other = _clean_spec(value, OTHER_LIMIT)
    elif value and kind and not size and not color and not other:
        other = _clean_spec(f"{kind}：{value}", OTHER_LIMIT)
    if label and other and not other.startswith(label):
        other = _clean_spec(f"{label}：{other}", OTHER_LIMIT)
    elif label and not size and not color and not other:
        other = label
    if not size and not color and not other:
        return None
    return {"size": size, "color": color, "other": other, "price": price}


def _collapse_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[tuple[str, str, str]] = []
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        key = _option_key(item)
        if key == ("", "", ""):
            continue
        if key not in found:
            found[key] = {"size": key[0], "color": key[1], "other": key[2], "price": item.get("price") or ""}
            order.append(key)
            continue
        if item.get("price"):
            found[key]["price"] = item["price"]
        if len(order) >= OPTION_LIMIT:
            break
    return [found[key] for key in order][:OPTION_LIMIT]


def _option_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _clean_spec(item.get("size")),
        _clean_spec(item.get("color")),
        _clean_spec(item.get("other"), OTHER_LIMIT),
    )


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


def _require_staff(actor: str) -> None:
    if not can_manage(actor):
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


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in raw and raw.get(key) is not None:
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


def _clean_spec(value: object, limit: int = SPEC_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


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


def _catalog_xlsx(rows: list[list[str]], image_rows: dict[int, tuple[str, bytes, str]]) -> bytes:
    media: list[tuple[str, bytes, str]] = []
    anchors: list[tuple[int, str]] = []
    seen: dict[str, str] = {}
    for excel_row in sorted(image_rows):
        filename, payload, extension = image_rows[excel_row]
        if filename not in seen:
            media_name = f"image{len(media) + 1}{extension}"
            seen[filename] = media_name
            media.append((media_name, payload, extension))
        anchors.append((excel_row, seen[filename]))
    sheet = _sheet_xml(rows, image_rows, bool(anchors))
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="貨品清單" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
    ]
    used_ext: set[str] = set()
    for _name, _payload, extension in media:
        if extension in used_ext:
            continue
        used_ext.add(extension)
        content_types.append(
            f'<Default Extension="{extension[1:]}" ContentType="{IMAGE_CONTENT_TYPES[extension]}"/>'
        )
    content_types.append(
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    )
    content_types.append(
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    )
    content_types.append(
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    )
    if anchors:
        content_types.append(
            '<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'
        )
    content_types.append("</Types>")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", "".join(content_types))
        book.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""",
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
""",
        )
        book.writestr("xl/workbook.xml", workbook)
        book.writestr("xl/styles.xml", styles)
        book.writestr("xl/worksheets/sheet1.xml", sheet)
        if anchors:
            book.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>
</Relationships>
""",
            )
            book.writestr("xl/drawings/drawing1.xml", _drawing_xml(anchors, media))
            book.writestr("xl/drawings/_rels/drawing1.xml.rels", _drawing_rels(media))
            for name, payload, _extension in media:
                book.writestr(f"xl/media/{name}", payload)
    return buffer.getvalue()


def _sheet_xml(rows: list[list[str]], image_rows: dict[int, tuple[str, bytes, str]], has_drawing: bool) -> str:
    xmlns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relation = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    root = f'<worksheet xmlns="{xmlns}"'
    if has_drawing:
        root += f' xmlns:r="{relation}"'
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        root + ">",
        '<sheetFormatPr defaultRowHeight="18"/>',
        '<cols>',
        '<col min="1" max="1" width="22" customWidth="1"/>',
        '<col min="2" max="2" width="18" customWidth="1"/>',
        '<col min="3" max="6" width="16" customWidth="1"/>',
        '<col min="7" max="9" width="18" customWidth="1"/>',
        "</cols>",
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        height = ' ht="78" customHeight="1"' if row_index in image_rows else ""
        cells = []
        for col_index, value in enumerate(row):
            if value == "":
                continue
            ref = f"{_column(col_index)}{row_index}"
            text = escape(value)
            cells.append(f'<c r="{ref}" t="inlineStr" s="1"><is><t xml:space="preserve">{text}</t></is></c>')
        parts.append(f'<row r="{row_index}"{height}>{"".join(cells)}</row>')
    parts.append("</sheetData>")
    if has_drawing:
        parts.append('<drawing r:id="rId1"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _drawing_xml(anchors: list[tuple[int, str]], media: list[tuple[str, bytes, str]]) -> str:
    rel_ids = {name: f"rId{index}" for index, (name, _payload, _extension) in enumerate(media, start=1)}
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"',
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"',
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
    ]
    # About 0.9 inch. The row is tall enough for the picture to sit in the image cell.
    extent = 822960
    for index, (excel_row, media_name) in enumerate(anchors, start=2):
        name = escape(media_name)
        rel = rel_ids[media_name]
        parts.append(
            "<xdr:oneCellAnchor>"
            "<xdr:from>"
            "<xdr:col>1</xdr:col><xdr:colOff>40000</xdr:colOff>"
            f"<xdr:row>{excel_row - 1}</xdr:row><xdr:rowOff>40000</xdr:rowOff>"
            "</xdr:from>"
            f'<xdr:ext cx="{extent}" cy="{extent}"/>'
            "<xdr:pic><xdr:nvPicPr>"
            f'<xdr:cNvPr id="{index}" name="{name}"/>'
            '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr>'
            "</xdr:nvPicPr><xdr:blipFill>"
            f'<a:blip r:embed="{rel}"/>'
            "<a:stretch><a:fillRect/></a:stretch>"
            "</xdr:blipFill><xdr:spPr><a:xfrm>"
            '<a:off x="0" y="0"/>'
            f'<a:ext cx="{extent}" cy="{extent}"/>'
            "</a:xfrm><a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>"
            "<xdr:clientData/>"
            "</xdr:oneCellAnchor>"
        )
    parts.append("</xdr:wsDr>")
    return "".join(parts)


def _drawing_rels(media: list[tuple[str, bytes, str]]) -> str:
    rows = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for index, (name, _payload, _extension) in enumerate(media, start=1):
        rows.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="../media/{escape(name)}"/>'
        )
    rows.append("</Relationships>")
    return "".join(rows)


def _column(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

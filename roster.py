"""Customer roster records. The portal never invents a name or a phone number."""

from __future__ import annotations

import copy
import json
import re
import secrets
import zipfile
from datetime import datetime
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from auth import STAFF

HK = ZoneInfo("Asia/Hong_Kong")

PURPOSE_ORDERS = "orders"
PURPOSE_CUSTOMERS = "customers"
PURPOSE_LABELS = {
    PURPOSE_ORDERS: "落單資料",
    PURPOSE_CUSTOMERS: "客戶資料",
}

SOURCE_CONVERSATION = "conversation"
SOURCE_SCREENSHOT = "contact_screenshot"
SOURCE_MANUAL = "manual"
SOURCE_LABELS = {
    SOURCE_CONVERSATION: "對話",
    SOURCE_SCREENSHOT: "聯絡人截圖",
    SOURCE_MANUAL: "人手輸入",
    "": "尚缺",
}
SOURCE_FROM_LABEL = {
    "對話": SOURCE_CONVERSATION,
    "聯絡人截圖": SOURCE_SCREENSHOT,
    "人手輸入": SOURCE_MANUAL,
    "尚缺": "",
}

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_PENDING: "待確認",
}

ROSTER_CONFIRM = "確定確認此顧客？"
ROSTER_EDIT_CONFIRM = "確定儲存此更正？"
ROSTER_DELETE_CONFIRM = "確定刪除此顧客？"

ROSTER_JSON_PATH = "customers/roster.json"
ROSTER_XLSX_PATH = "customers/roster.xlsx"
XLSX_HEADERS = ("顧客姓名", "電話", "號碼來源", "最後更新日期", "狀態", "備註／待確認")

CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SUBMISSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

NAME_LIMIT = 80
PHONE_LIMIT = 40
NOTE_LIMIT = 500
HISTORY_LIMIT = 30
CUSTOMER_LIMIT = 2000
CONFLICT_NOTE = "同名有不同電話，待超級管理員確認。"

BLANK_PHONE_WORDS = {"尚缺", "—", "-", "無", "没有", "沒有", "n/a", "na", "none"}


class RosterError(ValueError):
    """A roster change the page can show to staff."""


class RosterNotFound(RosterError):
    """No roster row has this id."""


def canonical_purpose(value: object) -> str:
    """Blank means the original order upload. Unknown values are refused."""
    if value is None:
        return PURPOSE_ORDERS
    if not isinstance(value, str):
        raise ValueError("請選擇上載用途。")
    text = value.strip()
    if not text:
        return PURPOSE_ORDERS
    if text not in PURPOSE_LABELS:
        raise ValueError("請選擇上載用途。")
    return text


def purpose_of(value: object) -> str:
    """Old manifests have no purpose and stay 落單資料."""
    if isinstance(value, str) and value.strip() in PURPOSE_LABELS:
        return value.strip()
    return PURPOSE_ORDERS


def empty_document() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "updated_by": "",
        "customers": [],
        "deleted": [],
    }


def phone_key(value: object) -> str:
    """Comparison key only. The stored phone keeps the original characters."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text.casefold() in BLANK_PHONE_WORDS:
        return ""
    out: list[str] = []
    for char in text:
        if char.isdigit():
            out.append(char)
        elif char == "+" and not out:
            out.append(char)
    key = "".join(out)
    if not any(char.isdigit() for char in key):
        return ""
    return key


def parse_roster_text(text: str) -> dict[str, Any] | None:
    """Return a roster document, or None when the file is not usable JSON."""
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
    customers: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_rows = data.get("customers")
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            row = normalize_customer(raw)
            if row is None:
                continue
            if row["id"] in seen:
                row["id"] = _new_id(seen)
            seen.add(row["id"])
            customers.append(row)
            if len(customers) >= CUSTOMER_LIMIT:
                break
    deleted = _normalize_deleted(data.get("deleted"))
    document = {
        "version": 1,
        "updated_at": _short_text(data.get("updated_at"), 40),
        "updated_by": _actor_name(data.get("updated_by")),
        "customers": customers,
        "deleted": deleted,
    }
    _enforce_conflicts(document)
    return document


def normalize_customer(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean_name(_first(raw, ("name", "顧客姓名")))
    source = canonical_source(_first(raw, ("phone_source", "號碼來源")))
    phones = _collect_phones(raw, source)
    note = _clean_note(_first(raw, ("note", "備註／待確認", "備註/待確認", "備註")))
    if not name and not phones:
        return None
    status = canonical_status(_first(raw, ("status", "狀態")))
    if len(phones) > 1:
        status = STATUS_PENDING
        if not note:
            note = CONFLICT_NOTE
    elif not status:
        status = STATUS_OK
    phone, phone_source = _selected_phone(phones, _clean_phone(_first(raw, ("phone", "電話"))), source)
    if len(phones) > 1:
        # Keep a previously stored number available, but do not choose a winner.
        phone_source = source if source and _same_source(phones, source) else ""
    customer_id = raw.get("id")
    if not isinstance(customer_id, str) or not CUSTOMER_ID_RE.fullmatch(customer_id.strip()):
        customer_id = _new_id(set())
    else:
        customer_id = customer_id.strip()
    return {
        "id": customer_id,
        "name": name,
        "phone": phone,
        "phone_source": phone_source,
        "phones": phones,
        "updated_at": _short_text(_first(raw, ("updated_at", "最後更新日期")), 40),
        "status": status,
        "note": note,
        "submission_ids": _submission_ids(raw.get("submission_ids") or raw.get("submissions")),
        "history": _history(raw.get("history")),
        "confirmed_by": _actor_name(raw.get("confirmed_by")),
        "confirmed_at": _short_text(raw.get("confirmed_at"), 40),
    }


def merge_rosters(local: object, published: object) -> dict[str, Any]:
    """Merge a bot publish into the roster already on the portal.

    A blank published phone never erases a known phone. Two different phones
    for the same name stay 待確認; this function does not choose one.
    Rows already deleted by Super Admin are not restored.
    """
    base = normalize_document(local)
    incoming = normalize_document(published) if published is not None else empty_document()
    deleted = _union_deleted(base["deleted"], incoming["deleted"])
    actor = incoming["updated_by"] or "scanner"
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    by_name: dict[str, str] = {}
    for row in base["customers"]:
        if _blocked(row, deleted):
            continue
        rows[row["id"]] = copy.deepcopy(row)
        order.append(row["id"])
        if row["name"] and row["name"] not in by_name:
            by_name[row["name"]] = row["id"]

    published_rows = [row for row in incoming["customers"] if not _blocked(row, deleted)]
    published_rows = _collapse_incoming(published_rows)
    for row in published_rows:
        if _blocked(row, deleted):
            continue
        match_id = _match_id(row, rows, by_name)
        if match_id is None:
            fresh = _as_new_row(row, actor)
            if _blocked(fresh, deleted):
                continue
            while fresh["id"] in rows:
                fresh["id"] = _new_id(set(rows))
            rows[fresh["id"]] = fresh
            order.append(fresh["id"])
            if fresh["name"] and fresh["name"] not in by_name:
                by_name[fresh["name"]] = fresh["id"]
            continue
        merged = _merge_pair(rows[match_id], row, actor)
        rows[match_id] = merged
        if merged["name"]:
            by_name[merged["name"]] = match_id

    document = {
        "version": 1,
        "updated_at": incoming["updated_at"] or base["updated_at"],
        "updated_by": incoming["updated_by"] or base["updated_by"],
        "customers": [rows[row_id] for row_id in order if row_id in rows],
        "deleted": deleted,
    }
    return normalize_document(document)


def apply_confirm(document: dict[str, Any], customer_id: str, actor: str, phone_choice: str) -> dict[str, Any]:
    _require_super(actor)
    doc = normalize_document(copy.deepcopy(document))
    row = _find(doc, customer_id)
    before = _snapshot(row)
    phones = _phones_of(row)
    choice = phone_choice.strip() if isinstance(phone_choice, str) else ""
    if len(phones) > 1:
        chosen = next((item for item in phones if item["phone"] == choice), None)
        if chosen is None:
            raise RosterError("請選擇要保留的電話，不會自動挑選。")
        row["phones"] = [dict(chosen)]
        row["phone"] = chosen["phone"]
        row["phone_source"] = chosen["source"]
    elif len(phones) == 1:
        row["phones"] = [dict(phones[0])]
        row["phone"] = phones[0]["phone"]
        row["phone_source"] = phones[0]["source"] or row["phone_source"]
    else:
        row["phones"] = []
        row["phone"] = ""
        row["phone_source"] = ""
    now = _now()
    row["status"] = STATUS_OK
    row["confirmed_by"] = actor
    row["confirmed_at"] = now
    row["updated_at"] = now
    _enforce_conflicts(doc, keep_ok_id=row["id"])
    _append_history(row, actor, "confirm", "", before, _snapshot(row))
    _touch(doc, actor, now)
    return doc


def apply_edit(
    document: dict[str, Any],
    customer_id: str,
    actor: str,
    name: str,
    phone: str,
    note: str,
) -> dict[str, Any]:
    _require_super(actor)
    cleaned_note = _clean_note(note)
    if not cleaned_note:
        raise RosterError("請填寫更正說明。")
    if len(cleaned_note) > NOTE_LIMIT:
        raise RosterError("更正說明太長。")
    cleaned_name = _clean_name(name)
    if len(cleaned_name) > NAME_LIMIT:
        raise RosterError("姓名太長。")
    cleaned_phone = _clean_phone(phone)
    if len(cleaned_phone) > PHONE_LIMIT:
        raise RosterError("電話太長。")
    if cleaned_phone and not phone_key(cleaned_phone):
        raise RosterError("電話需保留原本的號碼，不能改成沒有數字的文字。")
    doc = normalize_document(copy.deepcopy(document))
    row = _find(doc, customer_id)
    if phone_key(row.get("phone")) and not phone_key(cleaned_phone):
        raise RosterError("已有電話不可改成空白。")
    name_changed = cleaned_name != row["name"]
    phone_changed = cleaned_phone != (row.get("phone") or "")
    if not name_changed and not phone_changed and cleaned_note == row["note"]:
        raise RosterError("沒有需要儲存的更正。")
    before = _snapshot(row)
    if phone_changed:
        if cleaned_phone:
            row["phone"] = cleaned_phone
            row["phone_source"] = SOURCE_MANUAL
            row["phones"] = [{"phone": cleaned_phone, "source": SOURCE_MANUAL}]
        else:
            row["phone"] = ""
            row["phone_source"] = ""
            row["phones"] = []
        row["status"] = STATUS_OK
        row["confirmed_by"] = ""
        row["confirmed_at"] = ""
    elif name_changed:
        # A name correction is a staff edit. Keep every known phone; do not pick one.
        row["phone_source"] = SOURCE_MANUAL if row.get("phone") else row.get("phone_source") or ""
        for item in row["phones"]:
            if row.get("phone") and phone_key(item["phone"]) == phone_key(row["phone"]):
                item["source"] = SOURCE_MANUAL
        if len(_phones_of(row)) <= 1:
            row["status"] = STATUS_OK
            row["confirmed_by"] = ""
            row["confirmed_at"] = ""
    row["name"] = cleaned_name
    row["note"] = cleaned_note
    now = _now()
    row["updated_at"] = now
    _enforce_conflicts(doc)
    current = _find(doc, customer_id)
    _append_history(current, actor, "edit", cleaned_note, before, _snapshot(current))
    _touch(doc, actor, now)
    return doc


def apply_delete(document: dict[str, Any], customer_id: str, actor: str) -> dict[str, Any]:
    _require_super(actor)
    doc = normalize_document(copy.deepcopy(document))
    row = _find(doc, customer_id)
    now = _now()
    tomb = {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "phones": [item["phone"] for item in _phones_of(row)],
        "deleted_by": actor,
        "deleted_at": now,
        "history": list(row["history"]),
    }
    _append_history(
        tomb,
        actor,
        "delete",
        "",
        _snapshot(row),
        {"name": "", "phone": "", "phones": [], "status": ""},
    )
    doc["customers"] = [item for item in doc["customers"] if item["id"] != row["id"]]
    doc["deleted"] = _union_deleted(doc["deleted"], [tomb])
    _touch(doc, actor, now)
    return normalize_document(doc)


def filter_customers(customers: list[dict[str, Any]], query: str, which: str) -> list[dict[str, Any]]:
    selected = which if which in {"all", "missing_phone", "pending"} else "all"
    needle = query.strip() if isinstance(query, str) else ""
    needle_key = phone_key(needle)
    folded = needle.casefold()
    found: list[dict[str, Any]] = []
    for row in customers:
        phones = _phones_of(row)
        if selected == "missing_phone" and phones:
            continue
        if selected == "pending" and row.get("status") != STATUS_PENDING:
            continue
        if folded:
            name = str(row.get("name") or "")
            blob = " ".join(item["phone"] for item in phones)
            digits = "".join(phone_key(item["phone"]) for item in phones)
            name_hit = folded in name.casefold()
            phone_hit = needle in blob or (bool(needle_key) and needle_key in digits)
            if not name_hit and not phone_hit:
                continue
        found.append(row)
    return found


def present_customer(row: dict[str, Any]) -> dict[str, Any]:
    phones = _phones_of(row)
    missing = not phones
    if missing:
        phone_display = "尚缺"
    elif len(phones) > 1 and row.get("status") == STATUS_PENDING:
        phone_display = " ／ ".join(item["phone"] for item in phones)
    else:
        phone_display = str(row.get("phone") or phones[0]["phone"])
    sources = []
    for item in phones:
        label = SOURCE_LABELS.get(item["source"], "尚缺")
        if label not in sources:
            sources.append(label)
    if not sources:
        source_label = "尚缺"
    elif len(sources) == 1:
        source_label = sources[0]
    else:
        source_label = "、".join(label for label in sources if label != "尚缺") or "尚缺"
    status = row.get("status") if row.get("status") in STATUS_LABELS else STATUS_OK
    name = str(row.get("name") or "")
    history = []
    for event in row.get("history") or []:
        if isinstance(event, dict):
            history.append(_present_history(event))
    return {
        "id": row.get("id") or "",
        "name": name,
        "name_display": name or "尚缺",
        "phone": str(row.get("phone") or ""),
        "phone_display": phone_display,
        "phone_missing": missing,
        "phone_source": str(row.get("phone_source") or ""),
        "phone_source_label": source_label,
        "phones": [
            {
                "phone": item["phone"],
                "source": item["source"],
                "source_label": SOURCE_LABELS.get(item["source"], "尚缺"),
            }
            for item in phones
        ],
        "conflict": len(phones) > 1,
        "updated_at": str(row.get("updated_at") or ""),
        "updated_display": _format_time(str(row.get("updated_at") or "")),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "note": str(row.get("note") or ""),
        "submission_ids": list(row.get("submission_ids") or []),
        "history": history,
        "confirmed_by": str(row.get("confirmed_by") or ""),
        "confirmed_display": _format_time(str(row.get("confirmed_at") or "")),
    }


def roster_xlsx_bytes(document: dict[str, Any]) -> bytes:
    rows = [present_customer(row) for row in normalize_document(document)["customers"]]
    sheet_rows = [list(XLSX_HEADERS)]
    for row in rows:
        sheet_rows.append(
            [
                row["name_display"],
                row["phone_display"],
                row["phone_source_label"],
                row["updated_display"],
                row["status_label"],
                row["note"],
            ]
        )
    return _xlsx(sheet_rows)


def canonical_source(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text in SOURCE_LABELS and text != "尚缺":
        return text
    return SOURCE_FROM_LABEL.get(text, "")


def canonical_status(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text in {STATUS_OK, "正常"}:
        return STATUS_OK
    if text in {STATUS_PENDING, "待確認"}:
        return STATUS_PENDING
    return ""


def _require_super(actor: str) -> None:
    if STAFF.get(actor) != "super_admin":
        raise PermissionError("沒有權限。")


def _find(document: dict[str, Any], customer_id: str) -> dict[str, Any]:
    if not isinstance(customer_id, str) or not CUSTOMER_ID_RE.fullmatch(customer_id):
        raise RosterNotFound("找不到這位顧客。")
    for row in document["customers"]:
        if row["id"] == customer_id:
            return row
    raise RosterNotFound("找不到這位顧客。")


def _collect_phones(raw: dict[str, Any], source: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    direct = _clean_phone(_first(raw, ("phone", "電話")))
    if phone_key(direct):
        found.append({"phone": direct, "source": source})
    extra = raw.get("phones")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str):
                phone = _clean_phone(item)
                item_source = source
            elif isinstance(item, dict):
                phone = _clean_phone(item.get("phone"))
                item_source = canonical_source(item.get("source")) or source
            else:
                continue
            if phone_key(phone):
                found.append({"phone": phone, "source": item_source})
    return _unique_phones(found)


def _unique_phones(items: list[dict[str, str]]) -> list[dict[str, str]]:
    order: list[str] = []
    found: dict[str, dict[str, str]] = {}
    for item in items:
        key = phone_key(item.get("phone"))
        if not key:
            continue
        source = item.get("source") if item.get("source") in SOURCE_FROM_LABEL.values() else ""
        if key not in found:
            found[key] = {"phone": item["phone"], "source": source}
            order.append(key)
        elif source and not found[key]["source"]:
            found[key]["source"] = source
    return [found[key] for key in order]


def _phones_of(row: dict[str, Any]) -> list[dict[str, str]]:
    return _collect_phones(row, canonical_source(row.get("phone_source")))


def _selected_phone(
    phones: list[dict[str, str]], explicit: str, source: str
) -> tuple[str, str]:
    if len(phones) == 1:
        return phones[0]["phone"], phones[0]["source"] or source
    if len(phones) > 1:
        key = phone_key(explicit)
        for item in phones:
            if key and phone_key(item["phone"]) == key:
                return item["phone"], item["source"]
        return "", ""
    return "", ""


def _same_source(phones: list[dict[str, str]], source: str) -> bool:
    return bool(source) and all(not item["source"] or item["source"] == source for item in phones)


def _collapse_incoming(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    blank: list[dict[str, Any]] = []
    for row in rows:
        if not row["name"]:
            blank.append(copy.deepcopy(row))
            continue
        current = grouped.get(row["name"])
        if current is None:
            grouped[row["name"]] = copy.deepcopy(row)
            order.append(row["name"])
            continue
        grouped[row["name"]] = _merge_pair(current, row, current.get("updated_by") or "scanner")
    return [grouped[name] for name in order] + blank


def _merge_pair(base: dict[str, Any], incoming: dict[str, Any], actor: str) -> dict[str, Any]:
    before = _snapshot(base)
    phones = _unique_phones(_phones_of(base) + _phones_of(incoming))
    row = copy.deepcopy(base)
    row["name"] = incoming["name"] or base["name"]
    row["phones"] = phones
    if len(phones) == 1:
        row["phone"] = phones[0]["phone"]
        row["phone_source"] = phones[0]["source"] or base["phone_source"]
    elif len(phones) > 1:
        old_key = phone_key(base.get("phone"))
        kept = next((item for item in phones if phone_key(item["phone"]) == old_key), None) if old_key else None
        if kept is not None:
            row["phone"] = kept["phone"]
            row["phone_source"] = kept["source"]
        else:
            row["phone"] = ""
            row["phone_source"] = ""
    else:
        row["phone"] = ""
        row["phone_source"] = ""
    if len(phones) > 1 or base["status"] == STATUS_PENDING or incoming["status"] == STATUS_PENDING:
        row["status"] = STATUS_PENDING
    else:
        row["status"] = STATUS_OK
    if len(phones) > 1 and not (base["note"] or incoming["note"]):
        row["note"] = CONFLICT_NOTE
    elif base["note"]:
        row["note"] = base["note"]
    else:
        row["note"] = incoming["note"]
    row["submission_ids"] = _limit_ids(list(base["submission_ids"]) + list(incoming["submission_ids"]))
    if incoming["updated_at"]:
        row["updated_at"] = incoming["updated_at"]
    after = _snapshot(row)
    if before != after:
        _append_history(row, actor or "scanner", "publish", "", before, after)
    return row


def _as_new_row(row: dict[str, Any], actor: str) -> dict[str, Any]:
    fresh = copy.deepcopy(row)
    if not CUSTOMER_ID_RE.fullmatch(fresh["id"]):
        fresh["id"] = _new_id(set())
    if not fresh["updated_at"]:
        fresh["updated_at"] = _now()
    if not fresh["history"]:
        _append_history(fresh, actor or "scanner", "publish", "", _empty_snapshot(), _snapshot(fresh))
    return fresh


def _match_id(
    row: dict[str, Any], rows: dict[str, dict[str, Any]], by_name: dict[str, str]
) -> str | None:
    if row["id"] in rows:
        return row["id"]
    if row["name"] and row["name"] in by_name:
        return by_name[row["name"]]
    if row["name"]:
        return None
    incoming_keys = {phone_key(item["phone"]) for item in _phones_of(row)}
    if not incoming_keys:
        return None
    for existing in rows.values():
        if existing["name"]:
            continue
        if incoming_keys & {phone_key(item["phone"]) for item in _phones_of(existing)}:
            return existing["id"]
    return None


def _blocked(row: dict[str, Any], deleted: list[dict[str, Any]]) -> bool:
    keys = {phone_key(item["phone"]) for item in _phones_of(row)}
    for tomb in deleted:
        if row.get("id") and row["id"] == tomb.get("id"):
            return True
        if not row.get("name") or row["name"] != tomb.get("name"):
            continue
        tomb_keys = _tomb_keys(tomb)
        if keys == tomb_keys:
            return True
    return False


def _tomb_keys(tomb: dict[str, Any]) -> set[str]:
    keys = set()
    phone = tomb.get("phone")
    if phone_key(phone):
        keys.add(phone_key(phone))
    for item in tomb.get("phones") or []:
        if phone_key(item):
            keys.add(phone_key(item))
    return keys


def _enforce_conflicts(document: dict[str, Any], keep_ok_id: str | None = None) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in document["customers"]:
        phones = _phones_of(row)
        if len(phones) > 1 and row["status"] != STATUS_PENDING:
            row["status"] = STATUS_PENDING
            row["confirmed_by"] = ""
            row["confirmed_at"] = ""
        if len(phones) > 1 and not row["note"]:
            row["note"] = CONFLICT_NOTE
        if row["name"]:
            grouped.setdefault(row["name"], []).append(row)
    for group in grouped.values():
        keys: set[str] = set()
        for row in group:
            keys.update(phone_key(item["phone"]) for item in _phones_of(row))
        keys.discard("")
        if len(keys) <= 1:
            continue
        for row in group:
            if row["id"] == keep_ok_id and row["status"] == STATUS_OK:
                continue
            if row.get("confirmed_by") and row["status"] == STATUS_OK and len(_phones_of(row)) <= 1:
                continue
            row["status"] = STATUS_PENDING
            if not row["note"]:
                row["note"] = CONFLICT_NOTE


def _union_deleted(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(left) + list(right):
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "") + "|" + str(item.get("name") or "") + "|" + str(item.get("phone") or "")
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
        customer_id = item.get("id")
        if not isinstance(customer_id, str) or not CUSTOMER_ID_RE.fullmatch(customer_id.strip()):
            customer_id = ""
        else:
            customer_id = customer_id.strip()
        phones = []
        for phone in item.get("phones") or []:
            if isinstance(phone, str) and phone_key(phone):
                phones.append(_clean_phone(phone))
        deleted.append(
            {
                "id": customer_id,
                "name": _clean_name(item.get("name")),
                "phone": _clean_phone(item.get("phone")),
                "phones": phones,
                "deleted_by": _actor_name(item.get("deleted_by")),
                "deleted_at": _short_text(item.get("deleted_at"), 40),
                "history": _history(item.get("history")),
            }
        )
    return deleted[-500:]


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name") or "",
        "phone": row.get("phone") or "",
        "phones": [item["phone"] for item in _phones_of(row)],
        "status": row.get("status") or "",
    }


def _empty_snapshot() -> dict[str, Any]:
    return {"name": "", "phone": "", "phones": [], "status": ""}


def _append_history(
    row: dict[str, Any],
    actor: str,
    action: str,
    note: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    history = list(row.get("history") or [])
    history.append(
        {
            "at": _now(),
            "by": _actor_name(actor) or actor or "scanner",
            "action": action,
            "note": _clean_note(note),
            "before": before,
            "after": after,
        }
    )
    row["history"] = history[-HISTORY_LIMIT:]


def _history(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    events = []
    for item in raw[-HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action not in {"confirm", "edit", "delete", "publish", "conflict"}:
            continue
        events.append(
            {
                "at": _short_text(item.get("at"), 40),
                "by": _actor_name(item.get("by")) or "scanner",
                "action": action,
                "note": _clean_note(item.get("note")),
                "before": _snapshot_dict(item.get("before")),
                "after": _snapshot_dict(item.get("after")),
            }
        )
    return events


def _snapshot_dict(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_snapshot()
    phones = []
    for phone in raw.get("phones") or []:
        if isinstance(phone, str) and phone_key(phone):
            phones.append(_clean_phone(phone))
    return {
        "name": _clean_name(raw.get("name")),
        "phone": _clean_phone(raw.get("phone")),
        "phones": phones,
        "status": canonical_status(raw.get("status")),
    }


def _present_history(event: dict[str, Any]) -> dict[str, str]:
    labels = {
        "confirm": "確認",
        "edit": "更正",
        "delete": "刪除",
        "publish": "掃描發布",
        "conflict": "標成待確認",
    }
    before = event.get("before") or {}
    after = event.get("after") or {}
    parts = [f"由 {event.get('by') or '—'} 於 {_format_time(str(event.get('at') or ''))} {labels.get(event.get('action'), '更新')}"]
    before_phones = before.get("phones") or ([before["phone"]] if before.get("phone") else [])
    after_phones = after.get("phones") or ([after["phone"]] if after.get("phone") else [])
    if before_phones != after_phones:
        parts.append("電話 " + ("、".join(before_phones) or "尚缺") + " → " + ("、".join(after_phones) or "尚缺"))
    if before.get("name") != after.get("name") and (before.get("name") or after.get("name")):
        parts.append("姓名 " + (before.get("name") or "尚缺") + " → " + (after.get("name") or "尚缺"))
    if event.get("note"):
        parts.append("說明：" + str(event["note"]))
    return {"summary": "。".join(parts) + "。"}


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


def _clean_phone(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r", "").replace("\n", "").strip()
    if text.casefold() in BLANK_PHONE_WORDS:
        return ""
    return text[:PHONE_LIMIT]


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


def _new_id(used: set[str]) -> str:
    while True:
        customer_id = "c" + secrets.token_hex(4)
        if customer_id not in used:
            return customer_id


def _now() -> str:
    return datetime.now(HK).replace(microsecond=0).isoformat()


def _touch(document: dict[str, Any], actor: str, now: str) -> None:
    document["updated_at"] = now
    document["updated_by"] = actor


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


def _xlsx(rows: list[list[str]]) -> bytes:
    sheet = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            ref = f"{_column(col_index)}{row_index}"
            text = escape(value)
            cells.append(
                f'<c r="{ref}" t="inlineStr" s="1"><is><t xml:space="preserve">{text}</t></is></c>'
            )
        sheet.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet.append("</sheetData></worksheet>")
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
  <sheets><sheet name="顧客名冊" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
""",
        )
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
        book.writestr("xl/worksheets/sheet1.xml", "".join(sheet))
    return buffer.getvalue()


def _column(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

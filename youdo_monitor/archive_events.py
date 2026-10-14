"""Canonical event schemas, content hashing, and safe field extractors for YouDo archive."""
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

SCHEMA_VERSION: int = 1

TASK_PAYLOAD_ALLOWLIST: Set[str] = {
    "title",
    "description",
    "budget",
    "budget_source",
    "category",
    "url",
    "offers_count",
    "is_in_progress",
    "status_flag",
    "is_virtual",
    "is_b2b",
    "created_date",
    "subcategory_name",
}

OFFER_PAYLOAD_ALLOWLIST: Set[str] = {
    "success",
    "task_id",
    "price",
    "offer_id",
    "status_code",
    "message",
}


def make_event(
    kind: str,
    task_id: int,
    payload: Dict[str, Any],
    *,
    observed_at: str,
    source: str,
    completeness: str,
    identity: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a canonical event dictionary with deterministic SHA-256 event_id.

    The event_id is calculated from (schema_version, kind, task_id, source,
    completeness, identity or payload) formatted as canonical UTF-8 JSON.
    observed_at is explicitly excluded from the content identity hash so
    re-observing identical task state does not produce conflicting event IDs.
    """
    normalized_task_id = int(task_id)
    payload_dict = payload if isinstance(payload, dict) else dict(payload)

    # Content identity tuple for canonical hashing
    ident_target = identity if identity is not None else payload_dict
    canonical_repr = (
        SCHEMA_VERSION,
        str(kind),
        normalized_task_id,
        str(source),
        str(completeness),
        ident_target,
    )
    canonical_json = json.dumps(
        canonical_repr,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    event_id = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    event: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "task_id": normalized_task_id,
        "kind": str(kind),
        "observed_at": str(observed_at),
        "source": str(source),
        "completeness": str(completeness),
        "payload": payload_dict,
    }
    if identity is not None:
        event["identity"] = str(identity)

    return event


def task_payload(summary: Dict[str, Any], detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract safe task metadata strictly conforming to TASK_PAYLOAD_ALLOWLIST.

    Explicitly excludes creator name, reviews, phones, emails, and profile objects.
    When detail is missing or contains an error, missing fields are preserved as None.
    """
    summary = summary or {}
    has_valid_detail = (detail is not None) and not detail.get("error")
    td = (
        (detail.get("TaskData") or (detail.get("ResultObject") or {}).get("TaskData") or detail)
        if has_valid_detail and isinstance(detail, dict)
        else {}
    )

    # Title
    title = (
        summary.get("title")
        or summary.get("name")
        or summary.get("Title")
        or summary.get("Name")
    )
    if title is None and td:
        title = td.get("title") or td.get("name") or td.get("Title") or td.get("Name")
    title_val = str(title) if title is not None else None

    # Description (only from valid detail, never fabricated or truncated)
    description_val: Optional[str] = None
    if has_valid_detail and td:
        desc = td.get("description") if td.get("description") is not None else td.get("Description")
        if desc is not None:
            description_val = str(desc)

    # Budget & budget_source
    budget_val: Optional[str] = None
    budget_source_val: Optional[str] = None

    if summary.get("budget") is not None:
        budget_val = str(summary["budget"])
        budget_source_val = summary.get("budget_source") or "feed"
    elif summary.get("BudgetDescription") is not None:
        budget_val = str(summary["BudgetDescription"])
        budget_source_val = summary.get("budget_source") or "feed"
    elif summary.get("budget_description") is not None:
        budget_val = str(summary["budget_description"])
        budget_source_val = summary.get("budget_source") or "feed"
    elif summary.get("Price") is not None:
        budget_val = str(summary["Price"])
        budget_source_val = summary.get("budget_source") or "feed"
    elif has_valid_detail and td:
        price_obj = td.get("Price") or {}
        if isinstance(price_obj, dict) and price_obj.get("TotalPrice") is not None:
            budget_val = str(price_obj["TotalPrice"])
            budget_source_val = "detail"
        elif td.get("budget") is not None:
            budget_val = str(td["budget"])
            budget_source_val = "detail"

    # Category
    cat = (
        summary.get("category")
        if summary.get("category") is not None
        else summary.get("CategoryFlag")
    )
    if cat is None and td:
        cat = td.get("category") or td.get("category_name") or (td.get("CategoryInfo") or {}).get("Name")
    category_val = cat

    # URL
    url_val = summary.get("url") or summary.get("Url")
    if not url_val and td:
        url_val = td.get("url") or td.get("Url")
    if not url_val:
        tid = summary.get("id") or summary.get("Id") or (td.get("id") or td.get("Id"))
        if tid is not None:
            url_val = f"/t{tid}"

    # Offers count
    offers_count_val: Optional[int] = None
    if has_valid_detail and td:
        oc = td.get("offers_count") if td.get("offers_count") is not None else td.get("OffersCount")
        if oc is not None:
            try:
                offers_count_val = int(oc)
            except (ValueError, TypeError):
                offers_count_val = None

    # Is in progress
    is_in_progress_val: Optional[bool] = None
    if has_valid_detail and td:
        prog = td.get("is_in_progress") if td.get("is_in_progress") is not None else td.get("IsInProgress")
        if prog is not None:
            is_in_progress_val = bool(prog)

    # Status flag
    status_flag_val: Optional[int] = None
    if has_valid_detail and td:
        sf = td.get("status_flag")
        if sf is None:
            status_obj = td.get("TaskStatus") or {}
            if isinstance(status_obj, dict):
                sf = status_obj.get("Flag")
        if sf is not None:
            try:
                status_flag_val = int(sf)
            except (ValueError, TypeError):
                status_flag_val = None

    # Is virtual
    iv = None
    if has_valid_detail and td:
        iv = td.get("is_virtual") if td.get("is_virtual") is not None else td.get("IsVirtual")
    if iv is None:
        iv = summary.get("is_virtual") if summary.get("is_virtual") is not None else summary.get("IsVirtual")
    is_virtual_val = bool(iv) if iv is not None else None

    # Is B2B
    ib = None
    if has_valid_detail and td:
        ib = td.get("is_b2b") if td.get("is_b2b") is not None else td.get("IsB2B")
    if ib is None:
        ib = summary.get("is_b2b") if summary.get("is_b2b") is not None else summary.get("IsB2B")
    is_b2b_val = bool(ib) if ib is not None else None

    # Created date
    cd = None
    if has_valid_detail and td:
        cd = td.get("created_date") or (td.get("Dates") or {}).get("CreationDateTime")
    if cd is None:
        cd = summary.get("created_date") or summary.get("date_string") or summary.get("DateTimeString")
    created_date_val = str(cd) if cd is not None else None

    # Subcategory name
    sub = None
    if has_valid_detail and td:
        sub = td.get("subcategory_name") or (td.get("SubcategoryInfo") or {}).get("Name")
    if sub is None:
        sub = summary.get("subcategory_name")
    subcategory_name_val = str(sub) if sub is not None else None

    return {
        "budget": budget_val,
        "budget_source": budget_source_val,
        "category": category_val,
        "created_date": created_date_val,
        "description": description_val,
        "is_b2b": is_b2b_val,
        "is_in_progress": is_in_progress_val,
        "is_virtual": is_virtual_val,
        "offers_count": offers_count_val,
        "status_flag": status_flag_val,
        "subcategory_name": subcategory_name_val,
        "title": title_val,
        "url": url_val,
    }


def offer_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract safe offer response metadata strictly conforming to OFFER_PAYLOAD_ALLOWLIST.

    Never extracts CreatorInfo or personal profile objects.
    """
    result = result or {}
    success = bool(result.get("success", False))

    tid = result.get("task_id")
    task_id_val: Optional[int] = None
    if tid is not None:
        try:
            task_id_val = int(tid)
        except (ValueError, TypeError):
            task_id_val = None

    price_val = result.get("price")

    # Extract offer_id from response structure
    offer_id_val: Optional[int] = None
    if result.get("offer_id") is not None:
        try:
            offer_id_val = int(result["offer_id"])
        except (ValueError, TypeError):
            offer_id_val = None
    else:
        resp = result.get("response") or {}
        ro = resp.get("ResultObject") or result.get("ResultObject") or {}
        if isinstance(ro, dict):
            offer = ro.get("Offer") or {}
            if isinstance(offer, dict):
                oid = offer.get("Id") if offer.get("Id") is not None else offer.get("id")
                if oid is not None:
                    try:
                        offer_id_val = int(oid)
                    except (ValueError, TypeError):
                        offer_id_val = None
        if offer_id_val is None and isinstance(resp, dict):
            oid = resp.get("OfferId") or resp.get("offer_id")
            if oid is not None:
                try:
                    offer_id_val = int(oid)
                except (ValueError, TypeError):
                    offer_id_val = None

    # Extract status_code
    sc = result.get("status_code") if result.get("status_code") is not None else result.get("status")
    if sc is None and isinstance(result.get("response"), dict):
        sc = result["response"].get("status") or result["response"].get("status_code")
    status_code_val: Optional[int] = None
    if sc is not None:
        try:
            status_code_val = int(sc)
        except (ValueError, TypeError):
            status_code_val = None

    # Extract safe message / error
    msg = result.get("message") or result.get("error") or result.get("reason")
    if msg is None and isinstance(result.get("response"), dict):
        msg = result["response"].get("Message") or result["response"].get("message")
    message_val = str(msg) if msg is not None else None

    return {
        "message": message_val,
        "offer_id": offer_id_val,
        "price": price_val,
        "status_code": status_code_val,
        "success": success,
        "task_id": task_id_val,
    }


def rules_fingerprint(paths: List[Path]) -> str:
    """Compute deterministic SHA-256 hash of sorted concatenated file contents."""
    sorted_paths = sorted(paths, key=lambda p: str(p))
    hasher = hashlib.sha256()
    for p in sorted_paths:
        path_obj = Path(p)
        hasher.update(path_obj.read_bytes())
    return hasher.hexdigest()


def task_completeness(detail: Optional[Dict[str, Any]]) -> str:
    """Determine observation completeness flag ('detail' vs 'partial')."""
    if not detail or detail.get("error"):
        return "partial"
    td = (detail.get("TaskData") or (detail.get("ResultObject") or {}).get("TaskData") or detail)
    if not isinstance(td, dict):
        return "partial"
    desc = td.get("description") if td.get("description") is not None else td.get("Description")
    if desc is None:
        return "partial"
    return "detail"


def get_code_revision(repo_dir: Optional[Path] = None) -> str:
    """Return the git HEAD commit SHA or 'unknown' without external shell dependencies."""
    try:
        root = repo_dir or Path(__file__).resolve().parent.parent
        git_head = root / ".git" / "HEAD"
        if not git_head.exists():
            return "unknown"
        ref = git_head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = root / ".git" / ref[5:]
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()
            packed_refs = root / ".git" / "packed-refs"
            if packed_refs.exists():
                target = ref[5:]
                for line in packed_refs.read_text(encoding="utf-8").splitlines():
                    if line.endswith(target):
                        return line.split()[0]
        return ref
    except Exception:
        return "unknown"

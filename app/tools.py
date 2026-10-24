"""Tools the complaint agent can call.

The model only chooses which tool to call and with what arguments. Anything
that ends up in a ticket (category, department, ward, location) is checked
here against data the server produced itself, so a confused or manipulated
model can't file a made-up complaint. There is deliberately no tool that
changes a ticket's status or hands out reward points.
"""
import json
import logging
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from google.adk.tools import ToolContext

from . import geo, rewards, trust
from .config import DATA_DIR, settings
from .images import hash_distance
from .store import Store, expected_by, now

log = logging.getLogger(__name__)

DEPARTMENTS: dict = json.loads((DATA_DIR / "departments.json").read_text())
TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TICKET_RE = re.compile(r"^AMC-[A-HJ-NP-Z2-9]{6}$")
IMAGE_RE = re.compile(r"^[0-9a-f]{24}$")
AMC_HANDLE = "@AmdavadAMC"
REUSE_WINDOW_DAYS = 30
REUSE_MAX_DISTANCE = 5  # bits out of 64; re-saved or slightly cropped copies stay under this


@dataclass
class Services:
    store: Store
    vision: object  # anything with analyze(bytes) -> dict
    maps_api_key: str = ""


_services: Services | None = None


def configure(services: Services) -> None:
    global _services
    _services = services


def svc() -> Services:
    if _services is None:
        raise RuntimeError("tools not configured")
    return _services


def new_ticket_id() -> str:
    return "AMC-" + "".join(secrets.choice(TICKET_ALPHABET) for _ in range(6))


def _own_image(image_id: str, tool_context: ToolContext) -> dict | None:
    image_id = str(image_id).strip()
    if not IMAGE_RE.match(image_id):
        return None
    image = svc().store.get_image(image_id)
    if not image or image["session_id"] != tool_context.user_id:
        return None
    return image


def _remember_ward(tool_context: ToolContext, ward: str) -> None:
    # kept in the store too, so a follow-up message served by another
    # instance can still file the complaint
    found = list(tool_context.state.get("resolved_wards", []))
    if ward not in found:
        found.append(ward)
    tool_context.state["resolved_wards"] = found
    svc().store.add_resolved_ward(tool_context.user_id, ward)


def _resolved_wards(tool_context: ToolContext) -> set[str]:
    return set(tool_context.state.get("resolved_wards", [])) | set(
        svc().store.get_resolved_wards(tool_context.user_id))


def analyze_photo(image_id: str, tool_context: ToolContext) -> dict:
    """Looks at the uploaded photo and identifies the civic issue in it.

    Args:
        image_id: The id of the uploaded photo.

    Returns:
        category (pothole, garbage, streetlight, waterlogging, other_civic or
        not_civic), severity, a short description and a confidence from 0 to 1.
    """
    image = _own_image(image_id, tool_context)
    if not image:
        return {"error": "Unknown photo"}
    if image.get("analysis"):
        return image["analysis"]
    try:
        analysis = svc().vision.analyze(Path(image["path"]).read_bytes())
    except Exception:
        log.exception("image analysis failed")
        return {"error": "Could not analyse the photo right now"}
    svc().store.set_image_analysis(image["id"], analysis)
    return analysis


def get_photo_location(image_id: str, tool_context: ToolContext) -> dict:
    """Returns the GPS location of the report (from the photo or the user's device), if any.

    Args:
        image_id: The id of the uploaded photo.
    """
    image = _own_image(image_id, tool_context)
    if not image:
        return {"error": "Unknown photo"}
    if image.get("lat") is None:
        return {"has_location": False}
    return {"has_location": True, "latitude": image["lat"], "longitude": image["lng"]}


def find_ward_by_coordinates(latitude: float, longitude: float, tool_context: ToolContext) -> dict:
    """Finds the AMC ward and zone for a GPS location.

    Args:
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
    """
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return {"found": False, "reason": "invalid coordinates"}
    ward = geo.nearest_ward(lat, lng, settings.max_ward_distance_km)
    if not ward:
        return {"found": False, "reason": "This location is outside AMC limits"}
    _remember_ward(tool_context, ward["ward"])
    return {"found": True, **ward}


def find_ward_by_area(area_name: str, tool_context: ToolContext) -> dict:
    """Finds the AMC ward for an area, landmark or road name typed by the user.

    Args:
        area_name: Area, landmark or road, for example "CG Road" or "near Law Garden".
    """
    area_name = str(area_name)[:100]
    ward = geo.match_area(area_name)
    if not ward:
        coords = geo.geocode(area_name, svc().maps_api_key)
        if coords:
            ward = geo.nearest_ward(*coords, settings.max_ward_distance_km)
    if not ward:
        return {"found": False, "suggestions": geo.suggestions(area_name)}
    _remember_ward(tool_context, ward["ward"])
    return {"found": True, "ward": ward["ward"], "zone": ward["zone"]}


def get_department(category: str) -> dict:
    """Returns the AMC department responsible for a category of issue.

    Args:
        category: One of pothole, garbage, streetlight, waterlogging, other_civic.
    """
    dept = DEPARTMENTS.get(str(category))
    if not dept:
        return {"error": "No department handles this category"}
    return {"category": category, **dept}


def file_complaint(image_id: str, ward: str, area_name: str, confirmed_category: str,
                   tool_context: ToolContext) -> dict:
    """Files the complaint with AMC and returns the tracking ID.

    Only call this after analyze_photo and after the ward was found with
    find_ward_by_coordinates or find_ward_by_area.

    Args:
        image_id: The id of the uploaded photo.
        ward: The ward name returned by the ward lookup.
        area_name: The area the user typed, or an empty string.
        confirmed_category: Empty string normally. If the photo analysis had low
            confidence or said not_civic and the user then confirmed what the
            issue is, pass that category here.
    """
    store = svc().store
    image = _own_image(image_id, tool_context)
    if not image:
        return {"error": "Unknown photo"}
    analysis = image.get("analysis")
    if not analysis:
        return {"error": "Analyse the photo first"}

    category = analysis["category"]
    uncertain = category in ("not_civic", "other_civic") or analysis["confidence"] < settings.min_confidence
    if uncertain and confirmed_category:
        category = str(confirmed_category)
    if category not in DEPARTMENTS:
        return {"error": "This doesn't look like a civic issue. Ask the user what the problem is."}

    ward_info = geo.WARD_BY_NAME.get(str(ward).lower())
    if not ward_info or ward_info["ward"] not in _resolved_wards(tool_context):
        return {"error": "Look up the ward first with find_ward_by_coordinates or find_ward_by_area"}

    area_name = str(area_name or "")[:100]
    checked = trust.score(image.get("meta"), analysis, _photo_reused(image), now())
    trusted = checked["level"] == "trusted"
    since = now() - timedelta(hours=settings.duplicate_window_hours)
    for t in store.recent_tickets(category, since):
        near = (
            image.get("lat") is not None and t.get("lat") is not None
            and geo.haversine_km(image["lat"], image["lng"], t["lat"], t["lng"]) * 1000
            <= settings.duplicate_radius_m
        )
        same_spot = (
            t["ward"] == ward_info["ward"] and area_name
            and t.get("area_text") and t["area_text"].strip().lower() == area_name.strip().lower()
        )
        if near or same_spot:
            store.add_report(t["id"])
            store.set_last_ticket(tool_context.user_id, t["id"])
            store.set_pending_image(tool_context.user_id, None)
            tool_context.state["last_ticket_id"] = t["id"]
            reward = {"points": 0, "reason": "Photo needs verification"}
            if t["session_id"] == tool_context.user_id:
                reward = {"points": 0, "reason": "You already reported this issue"}
            elif trusted:
                reward = rewards.award(store, tool_context.user_id, t, "confirm")
            return {
                "duplicate": True,
                "ticket_id": t["id"],
                "status": t["status"],
                "report_count": t.get("report_count", 1) + 1,
                "message": "This issue was already reported here. Your report was added to it.",
                "trust_score": checked["score"],
                "trust_level": checked["level"],
                "points_awarded": reward["points"],
                "points_reason": reward["reason"],
            }

    dept = DEPARTMENTS[category]
    ticket = {
        "id": new_ticket_id(),
        "session_id": tool_context.user_id,
        "image_id": image["id"],
        "category": category,
        "severity": analysis["severity"],
        "description": analysis["description"],
        "ward": ward_info["ward"],
        "zone": ward_info["zone"],
        "department": dept["department"],
        "lat": image.get("lat"),
        "lng": image.get("lng"),
        "area_text": area_name or None,
        "status": "Filed",
        "history": [{"status": "Filed", "at": now().isoformat(), "note": "Complaint registered"}],
        "report_count": 1,
        "expected_by": expected_by(dept["sla_days"]),
        "created_at": now().isoformat(),
        "trust_score": checked["score"],
        "trust_level": checked["level"],
        "trust_reasons": checked["reasons"],
        "user_note": image.get("note"),
    }
    store.create_ticket(ticket)
    store.set_last_ticket(tool_context.user_id, ticket["id"])
    store.set_pending_image(tool_context.user_id, None)
    tool_context.state["last_ticket_id"] = ticket["id"]
    reward = (rewards.award(store, tool_context.user_id, ticket, "first_report") if trusted
              else {"points": 0, "reason": "Photo needs verification"})
    return {
        "duplicate": False,
        "ticket_id": ticket["id"],
        "ward": ticket["ward"],
        "department": ticket["department"],
        "status": "Filed",
        "expected_by": ticket["expected_by"],
        "trust_score": checked["score"],
        "trust_level": checked["level"],
        "points_awarded": reward["points"],
        "points_reason": reward["reason"],
    }


def _photo_reused(image: dict) -> bool:
    phash = (image.get("meta") or {}).get("phash")
    # near-blank images all hash alike, so they can't be compared meaningfully
    if not phash or not 8 <= bin(int(phash, 16)).count("1") <= 56:
        return False
    since = now() - timedelta(days=REUSE_WINDOW_DAYS)
    return any(other["id"] != image["id"] and other.get("phash")
               and hash_distance(phash, other["phash"]) <= REUSE_MAX_DISTANCE
               for other in svc().store.recent_image_hashes(since))


def public_view(ticket: dict) -> dict:
    """Fields that are safe to show anyone holding the tracking ID."""
    keys = ["id", "category", "severity", "description", "ward", "zone", "department",
            "status", "history", "report_count", "expected_by", "created_at",
            "trust_score", "trust_level", "trust_reasons"]
    return {k: ticket.get(k) for k in keys}


def get_complaint_status(ticket_id: str, tool_context: ToolContext) -> dict:
    """Gets the current status of a complaint.

    Args:
        ticket_id: The tracking ID like AMC-7F3K9Q, or an empty string for the
            user's most recent complaint.
    """
    ticket_id = str(ticket_id or "").strip().upper()
    if not ticket_id:
        ticket_id = (svc().store.get_last_ticket(tool_context.user_id)
                     or tool_context.state.get("last_ticket_id") or "")
        if not ticket_id:
            return {"error": "No complaint filed in this chat yet. Ask for the tracking ID."}
    if not TICKET_RE.match(ticket_id):
        return {"error": "Tracking IDs look like AMC-7F3K9Q"}
    ticket = svc().store.get_ticket(ticket_id)
    if not ticket:
        return {"error": "No complaint found with that tracking ID"}
    return public_view(ticket)


def draft_social_post(ticket_id: str, tool_context: ToolContext) -> dict:
    """Drafts a short public post about a filed complaint for X or Instagram.

    Args:
        ticket_id: The tracking ID, or an empty string for the user's latest complaint.
    """
    ticket_id = str(ticket_id or "").strip().upper()
    last = svc().store.get_last_ticket(tool_context.user_id) or tool_context.state.get("last_ticket_id")
    if not ticket_id:
        ticket_id = last or ""
    if not TICKET_RE.match(ticket_id):
        return {"error": "Tracking IDs look like AMC-7F3K9Q"}
    ticket = svc().store.get_ticket(ticket_id)
    # the post includes the reporter's own wording, so only for their complaints
    if not ticket or (ticket["session_id"] != tool_context.user_id and ticket_id != last):
        return {"error": "You can only draft posts for complaints filed in this chat"}
    return social_post(ticket)


def social_post(ticket: dict) -> dict:
    label = {
        "pothole": "Pothole", "garbage": "Garbage pile-up", "streetlight": "Broken streetlight",
        "waterlogging": "Waterlogging", "other_civic": "Civic issue",
    }.get(ticket["category"], "Civic issue")
    area = ticket.get("area_text")
    place = f"{area}, {ticket['ward']} ward" if area else f"{ticket['ward']} ward"
    ward_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", ticket["ward"])
    text = (
        f"{label} in {place}. Reported to {ticket['department']}, "
        f"tracking ID {ticket['id']}. Please take a look. {AMC_HANDLE} {ward_tag} #Ahmedabad #FixMyAmdavad"
    )
    return {
        "text": text,
        "x_url": "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(text),
    }


def get_my_rewards(tool_context: ToolContext) -> dict:
    """Returns the user's reward points, badges and recent awards."""
    return rewards.summary(svc().store, tool_context.user_id)


AGENT_TOOLS = [
    analyze_photo,
    get_photo_location,
    find_ward_by_coordinates,
    find_ward_by_area,
    get_department,
    file_complaint,
    get_complaint_status,
    draft_social_post,
    get_my_rewards,
]

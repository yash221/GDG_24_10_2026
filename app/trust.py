"""Photo trust score.

No single check can prove a photo is real, so this adds up several cheap
signals. A low score never blocks a complaint; it marks it "needs
verification" and stops it from earning reward points.
"""
from datetime import datetime

TRUSTED_AT = 50
BASE = 40


def score(meta: dict, analysis: dict, reused: bool, now: datetime) -> dict:
    meta = meta or {}
    reasons: list[tuple[str, int]] = []

    if meta.get("camera"):
        reasons.append(("Taken on a phone or camera", 20))
    if meta.get("taken_at"):
        try:
            age_days = (now.replace(tzinfo=None) - datetime.fromisoformat(meta["taken_at"])).days
        except ValueError:
            age_days = None
        if age_days is not None and age_days <= 2:
            reasons.append(("Photo taken in the last 2 days", 15))
        elif age_days is not None and age_days > 30:
            reasons.append(("Photo is more than a month old", -10))
    if meta.get("gps"):
        reasons.append(("Photo has its own GPS location", 15))
    if meta.get("location_mismatch"):
        reasons.append(("Photo location is far from where you are", -25))
    if meta.get("ai_metadata"):
        reasons.append(("File says it was made with an AI image tool", -60))

    source = (analysis or {}).get("source", "camera_photo")
    if source == "camera_photo":
        reasons.append(("Looks like a real street photo", 10))
    elif source in ("screenshot", "screen_photo"):
        reasons.append(("Looks like a screenshot or a photo of a screen", -30))
    elif source == "ai_generated":
        reasons.append(("Looks AI-generated", -50))

    if reused:
        reasons.append(("Same photo was already used in another report", -40))

    total = max(0, min(100, BASE + sum(delta for _, delta in reasons)))
    return {
        "score": total,
        "level": "trusted" if total >= TRUSTED_AT else "needs_verification",
        "reasons": [{"text": text, "points": delta} for text, delta in reasons],
    }

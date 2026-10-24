"""Citizen reward points.

Points follow outcomes, not uploads: the big reward comes when AMC marks a
complaint resolved, which a fake photo can't get. Points are only granted
here, from server-side checks; the agent has no tool that awards them.
"""
import hashlib
from datetime import timedelta

from .store import Store, now

POINTS = {
    "first_report": 20,   # trusted photo of an issue nobody had reported yet
    "confirm": 5,         # trusted photo confirming someone else's report
    "resolved": 40,       # AMC fixed the issue you reported
}
REASON_TEXT = {
    "first_report": "First to report this issue",
    "confirm": "Confirmed an existing report",
    "resolved": "Your reported issue was fixed",
}
DAILY_CAP = 100

BADGES = [
    ("first_report", "First Report", lambda s: s["reports"] >= 1),
    ("road_guardian", "Road Guardian", lambda s: s["by_category"].get("pothole", 0) >= 3),
    ("swachh_nagrik", "Swachh Nagrik", lambda s: s["by_category"].get("garbage", 0) >= 3),
    ("community_voice", "Community Voice", lambda s: s["confirms"] >= 3),
    ("problem_solver", "Problem Solver", lambda s: s["resolved"] >= 1),
]


def award(store: Store, session_id: str, ticket: dict, reason: str) -> dict:
    """Give points once per (session, ticket, reason), within the daily cap."""
    if store.has_reward(session_id, ticket["id"], reason):
        return {"points": 0, "reason": "Already rewarded"}
    since = now() - timedelta(hours=24)
    earned_today = sum(r["points"] for r in store.rewards_for(session_id) if r["created_at"] >= since.isoformat())
    points = min(POINTS[reason], max(0, DAILY_CAP - earned_today))
    if points == 0:
        return {"points": 0, "reason": "Daily points limit reached"}
    store.add_reward({
        "session_id": session_id,
        "ticket_id": ticket["id"],
        "reason": reason,
        "category": ticket["category"],
        "points": points,
        "created_at": now().isoformat(),
    })
    return {"points": points, "reason": REASON_TEXT[reason]}


def summary(store: Store, session_id: str) -> dict:
    history = store.rewards_for(session_id)
    stats = {"reports": 0, "confirms": 0, "resolved": 0, "by_category": {}}
    for r in history:
        if r["reason"] == "first_report":
            stats["reports"] += 1
            stats["by_category"][r["category"]] = stats["by_category"].get(r["category"], 0) + 1
        elif r["reason"] == "confirm":
            stats["confirms"] += 1
        elif r["reason"] == "resolved":
            stats["resolved"] += 1
    return {
        "total": sum(r["points"] for r in history),
        "badges": [name for _, name, rule in BADGES if rule(stats)],
        "recent": [{"points": r["points"], "reason": REASON_TEXT[r["reason"]], "ticket_id": r["ticket_id"]}
                   for r in sorted(history, key=lambda r: r["created_at"], reverse=True)[:5]],
    }


def public_label(session_id: str) -> str:
    """Stable anonymous name for the leaderboard; can't be turned back into the session id."""
    return "Citizen " + hashlib.sha256(session_id.encode()).hexdigest()[:4].upper()


def leaderboard(store: Store, limit: int = 5) -> list[dict]:
    return [{"name": public_label(sid), "points": pts} for sid, pts in store.top_reporters(limit)]

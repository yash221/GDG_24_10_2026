from datetime import datetime, timedelta

import pytest

from app import rewards, tools, trust
from app.config import settings
from app.images import hash_distance, process_upload
from app.store import now

from .conftest import Ctx, make_photo, report

LAW_GARDEN = (23.0265, 72.5605)
CG_ROAD = (23.0300, 72.5580)
ADMIN = {"X-Admin-Token": "test-admin-token-123"}


def upload(store, tmp_path, session_id, **photo_kwargs):
    img = process_upload(make_photo(**photo_kwargs), tmp_path / "up", settings.max_upload_bytes, 1280)
    store.save_image({**img, "session_id": session_id})
    return img


def file_report(store, tmp_path, session_id, gps=LAW_GARDEN, **photo_kwargs):
    store.create_session(session_id) if not store.session_exists(session_id) else None
    ctx = Ctx(session_id)
    img = upload(store, tmp_path, session_id, gps=gps, **photo_kwargs)
    tools.analyze_photo(img["id"], ctx)
    ward = tools.find_ward_by_coordinates(*gps, ctx)
    return tools.file_complaint(img["id"], ward["ward"], "", "", ctx)


# --- trust score -----------------------------------------------------------

def test_fresh_camera_photo_with_gps_is_trusted(services, store, tmp_path):
    res = file_report(store, tmp_path, "s1", seed=1, taken=datetime.now())
    assert res["trust_level"] == "trusted" and res["trust_score"] >= 90


def test_metadata_is_read_before_exif_is_stripped(tmp_path):
    img = process_upload(make_photo(gps=LAW_GARDEN, seed=2, taken=datetime.now(), software="Midjourney v7"),
                         tmp_path, settings.max_upload_bytes, 1280)
    meta = img["meta"]
    assert meta["camera"] and meta["gps"] and meta["taken_at"] and meta["ai_metadata"]


def test_ai_tool_in_metadata_needs_verification(services, store, tmp_path):
    res = file_report(store, tmp_path, "s1", seed=3, software="Stable Diffusion XL")
    assert res["trust_level"] == "needs_verification"
    assert res["points_awarded"] == 0


@pytest.mark.parametrize("source", ["screenshot", "screen_photo", "ai_generated"])
def test_gemini_flags_lower_trust(services, store, tmp_path, vision, source):
    vision.result = {**vision.result, "source": source}
    res = file_report(store, tmp_path, "s1", seed=4, camera=False, gps=CG_ROAD)
    assert res["trust_level"] == "needs_verification"


def test_old_photo_scores_lower_than_fresh_one():
    old = trust.score({"camera": True, "taken_at": (datetime.now() - timedelta(days=90)).isoformat()},
                      {"source": "camera_photo"}, False, now())
    fresh = trust.score({"camera": True, "taken_at": datetime.now().isoformat()},
                        {"source": "camera_photo"}, False, now())
    assert old["score"] < fresh["score"]


def test_location_mismatch_lowers_trust():
    ok = trust.score({"camera": True, "gps": True}, {"source": "camera_photo"}, False, now())
    far = trust.score({"camera": True, "gps": True, "location_mismatch": True}, {"source": "camera_photo"}, False, now())
    assert far["score"] == ok["score"] - 25


def test_reused_photo_is_detected(services, store, tmp_path):
    file_report(store, tmp_path, "s1", seed=5, gps=LAW_GARDEN)
    # same picture, different place and person, far enough not to be a duplicate ticket
    res = file_report(store, tmp_path, "s2", seed=5, gps=(22.9960, 72.6030))
    assert res["duplicate"] is False
    assert res["trust_level"] == "needs_verification"


def test_similar_but_different_photos_are_not_reuse(tmp_path):
    a = process_upload(make_photo(seed=6), tmp_path, settings.max_upload_bytes, 1280)["meta"]["phash"]
    b = process_upload(make_photo(seed=7), tmp_path, settings.max_upload_bytes, 1280)["meta"]["phash"]
    a2 = process_upload(make_photo(seed=6, size=(800, 600)), tmp_path, settings.max_upload_bytes, 1280)["meta"]["phash"]
    assert hash_distance(a, a2) <= tools.REUSE_MAX_DISTANCE
    assert hash_distance(a, b) > tools.REUSE_MAX_DISTANCE


def test_trust_shown_on_public_ticket(client, session_id):
    tid = report(client, session_id, make_photo(gps=LAW_GARDEN, seed=8)).json()["ticket"]["id"]
    body = client.get(f"/api/tickets/{tid}").json()
    assert body["trust_level"] in ("trusted", "needs_verification")
    assert isinstance(body["trust_reasons"], list) and body["trust_reasons"]


# --- rewards ---------------------------------------------------------------

def test_trusted_new_report_earns_points(services, store, tmp_path):
    res = file_report(store, tmp_path, "s1", seed=10, taken=datetime.now())
    assert res["points_awarded"] == rewards.POINTS["first_report"]
    summary = rewards.summary(store, "s1")
    assert summary["total"] == 20 and "First Report" in summary["badges"]


def test_confirming_someone_elses_report(services, store, tmp_path):
    first = file_report(store, tmp_path, "s1", seed=11)
    near = (LAW_GARDEN[0] + 0.0002, LAW_GARDEN[1])
    second = file_report(store, tmp_path, "s2", seed=12, gps=near)
    assert second["duplicate"] and second["ticket_id"] == first["ticket_id"]
    assert second["points_awarded"] == rewards.POINTS["confirm"]


def test_no_points_for_confirming_your_own_report(services, store, tmp_path):
    file_report(store, tmp_path, "s1", seed=13)
    near = (LAW_GARDEN[0] + 0.0002, LAW_GARDEN[1])
    again = file_report(store, tmp_path, "s1", seed=14, gps=near)
    assert again["duplicate"] and again["points_awarded"] == 0
    assert rewards.summary(store, "s1")["total"] == 20


def test_confirm_points_only_once_per_ticket(services, store, tmp_path):
    file_report(store, tmp_path, "s1", seed=15)
    near = (LAW_GARDEN[0] + 0.0002, LAW_GARDEN[1])
    file_report(store, tmp_path, "s2", seed=16, gps=near)
    again = file_report(store, tmp_path, "s2", seed=17, gps=near)
    assert again["points_awarded"] == 0
    assert rewards.summary(store, "s2")["total"] == 5


def test_daily_cap(services, store, tmp_path):
    store.create_session("s1")
    spots = [(23.0365, 72.5611), (23.0120, 72.5630), (23.0560, 72.5550), (23.0790, 72.5880),
             (23.1090, 72.5850), (23.0400, 72.5100), (23.0500, 72.5000)]
    results = [file_report(store, tmp_path, "s1", seed=100 + i, gps=g) for i, g in enumerate(spots)]
    assert sum(r["points_awarded"] for r in results) == rewards.DAILY_CAP
    assert results[-1]["points_reason"] == "Daily points limit reached"


def test_resolved_awards_reporter_once(app_env, session_id):
    client, store, *_ = app_env
    tid = report(client, session_id, make_photo(gps=LAW_GARDEN, seed=20)).json()["ticket"]["id"]
    for _ in range(2):
        client.post(f"/api/admin/tickets/{tid}/status", json={"status": "Resolved"}, headers=ADMIN)
    summary = rewards.summary(store, session_id)
    assert summary["total"] == rewards.POINTS["first_report"] + rewards.POINTS["resolved"]
    assert "Problem Solver" in summary["badges"]


def test_report_response_includes_rewards(client, session_id):
    body = report(client, session_id, make_photo(gps=LAW_GARDEN, seed=21)).json()
    assert body["rewards"]["total"] == 20


def test_rewards_endpoint_needs_real_session(client, session_id):
    assert client.post("/api/rewards", json={"session_id": session_id}).status_code == 200
    assert client.post("/api/rewards", json={"session_id": "x" * 30}).status_code == 401


def test_leaderboard_is_anonymous(client, session_id):
    report(client, session_id, make_photo(gps=LAW_GARDEN, seed=22))
    leaders = client.get("/api/leaderboard").json()["leaders"]
    assert leaders and leaders[0]["points"] == 20
    assert leaders[0]["name"].startswith("Citizen ")
    assert session_id not in str(leaders)


def test_agent_tools_cannot_grant_points():
    import inspect
    for fn in tools.AGENT_TOOLS:
        params = inspect.signature(fn).parameters
        assert "points" not in params and "reason" not in params
    src = inspect.getsource(tools.get_my_rewards)
    assert "rewards.award(" not in src and "add_reward" not in src


def test_live_camera_photo_is_trusted(client, session_id):
    # the in-page camera produces a plain JPEG with no EXIF at all
    r = client.post("/api/report", data={"session_id": session_id, "area_text": "CG Road", "captured_live": "true"},
                    files={"photo": ("camera.jpg", make_photo(seed=30, camera=False), "image/jpeg")})
    t = r.json()["ticket"]
    assert t["trust_level"] == "trusted"
    assert any("in-app camera" in reason["text"] for reason in t["trust_reasons"])


def test_upload_without_metadata_is_not_marked_live(client, session_id):
    r = client.post("/api/report", data={"session_id": session_id, "area_text": "Paldi"},
                    files={"photo": ("x.jpg", make_photo(seed=31, camera=False), "image/jpeg")})
    assert not any("in-app camera" in reason["text"] for reason in r.json()["ticket"]["trust_reasons"])

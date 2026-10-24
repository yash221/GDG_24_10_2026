import json

import pytest

from app import geo, tools
from app.config import settings
from app.images import process_upload

from .conftest import Ctx, make_photo

LAW_GARDEN = (23.0265, 72.5605)
CG_ROAD = (23.0300, 72.5580)


def upload(store, tmp_path, session_id, gps=None):
    img = process_upload(make_photo(gps=gps), tmp_path / "up", settings.max_upload_bytes, 1280)
    store.save_image({**img, "session_id": session_id})
    return img["id"]


def file_one(store, tmp_path, ctx, gps=LAW_GARDEN, area=""):
    image_id = upload(store, tmp_path, ctx.user_id, gps=gps)
    tools.analyze_photo(image_id, ctx)
    if gps:
        ward = tools.find_ward_by_coordinates(*gps, ctx)
    else:
        ward = tools.find_ward_by_area(area, ctx)
    return tools.file_complaint(image_id, ward["ward"], area, "", ctx)


@pytest.mark.parametrize("category,dept", [
    ("pothole", "Roads & Engineering"),
    ("garbage", "Solid Waste Management"),
    ("streetlight", "Light Department (Electrical)"),
    ("waterlogging", "Storm Water Drainage"),
    ("other_civic", "General Complaints Cell"),
])
def test_department_mapping(category, dept):
    assert tools.get_department(category)["department"] == dept


def test_department_rejects_not_civic():
    assert "error" in tools.get_department("not_civic")


def test_ward_by_gps(services):
    ctx = Ctx("s1")
    res = tools.find_ward_by_coordinates(*LAW_GARDEN, ctx)
    assert res["found"] and res["ward"] == "Navrangpura"


def test_ward_outside_amc(services):
    ctx = Ctx("s1")
    res = tools.find_ward_by_coordinates(23.2156, 72.6369, ctx)  # Gandhinagar
    assert res["found"] is False
    assert ctx.state.get("resolved_wards") is None


@pytest.mark.parametrize("text,ward", [
    ("near Law Garden", "Navrangpura"),
    ("CG road", "Navrangpura"),
    ("Satellite area", "Jodhpur"),
    ("opp Kankaria Lake", "Maninagar"),
    ("maninagar", "Maninagar"),
    ("navrangpur", "Navrangpura"),   # misspelled
    ("BODAKDEV", "Bodakdev"),
])
def test_ward_by_area(services, text, ward):
    res = tools.find_ward_by_area(text, Ctx("s1"))
    assert res["found"] and res["ward"] == ward


def test_unknown_area_gives_suggestions(services):
    res = tools.find_ward_by_area("Mumbai central", Ctx("s1"))
    assert res["found"] is False
    assert isinstance(res["suggestions"], list)


def test_every_ward_centre_maps_to_itself():
    for w in geo.WARDS:
        assert geo.nearest_ward(w["lat"], w["lng"], 6)["ward"] == w["ward"]


def test_full_filing_flow(services, store, tmp_path):
    ctx = Ctx("s1")
    res = file_one(store, tmp_path, ctx)
    assert res["duplicate"] is False
    assert tools.TICKET_RE.match(res["ticket_id"])
    assert res["department"] == "Roads & Engineering"
    assert res["ward"] == "Navrangpura"
    status = tools.get_complaint_status("", ctx)
    assert status["status"] == "Filed" and status["id"] == res["ticket_id"]


def test_analysis_is_cached(services, store, tmp_path, vision):
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1")
    tools.analyze_photo(image_id, ctx)
    tools.analyze_photo(image_id, ctx)
    assert vision.calls == 1


def test_cannot_file_without_ward_lookup(services, store, tmp_path):
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1", gps=LAW_GARDEN)
    tools.analyze_photo(image_id, ctx)
    res = tools.file_complaint(image_id, "Navrangpura", "", "", ctx)
    assert "error" in res


def test_cannot_file_with_made_up_ward(services, store, tmp_path):
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1", gps=LAW_GARDEN)
    tools.analyze_photo(image_id, ctx)
    tools.find_ward_by_coordinates(*LAW_GARDEN, ctx)
    assert "error" in tools.file_complaint(image_id, "Atlantis", "", "", ctx)
    assert "error" in tools.file_complaint(image_id, "Maninagar", "", "", ctx)  # real, but not looked up


def test_cannot_file_before_analysis(services, store, tmp_path):
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1", gps=LAW_GARDEN)
    tools.find_ward_by_coordinates(*LAW_GARDEN, ctx)
    assert "error" in tools.file_complaint(image_id, "Navrangpura", "", "", ctx)


def test_not_civic_photo_is_not_filed(services, store, tmp_path, vision):
    vision.result = {"category": "not_civic", "severity": "low", "description": "A cat", "confidence": 0.97}
    ctx = Ctx("s1")
    res = file_one(store, tmp_path, ctx)
    assert "error" in res


def test_confirmed_category_only_used_when_uncertain(services, store, tmp_path, vision):
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1", gps=LAW_GARDEN)
    tools.analyze_photo(image_id, ctx)
    tools.find_ward_by_coordinates(*LAW_GARDEN, ctx)
    # confident pothole: the model can't swap the category
    res = tools.file_complaint(image_id, "Navrangpura", "", "garbage", ctx)
    assert res["department"] == "Roads & Engineering"


def test_low_confidence_with_user_confirmation(services, store, tmp_path, vision):
    vision.result = {"category": "garbage", "severity": "medium", "description": "Dark photo", "confidence": 0.4}
    ctx = Ctx("s1")
    image_id = upload(store, tmp_path, "s1", gps=LAW_GARDEN)
    tools.analyze_photo(image_id, ctx)
    tools.find_ward_by_coordinates(*LAW_GARDEN, ctx)
    res = tools.file_complaint(image_id, "Navrangpura", "", "streetlight", ctx)
    assert res["department"] == "Light Department (Electrical)"
    assert "error" in tools.file_complaint(image_id, "Navrangpura", "", "not_civic", ctx)


def test_duplicate_nearby_report_is_merged(services, store, tmp_path):
    first = file_one(store, tmp_path, Ctx("s1"), gps=LAW_GARDEN)
    near = (LAW_GARDEN[0] + 0.0002, LAW_GARDEN[1])  # ~22 m away
    second = file_one(store, tmp_path, Ctx("s2"), gps=near)
    assert second["duplicate"] is True
    assert second["ticket_id"] == first["ticket_id"]
    assert store.get_ticket(first["ticket_id"])["report_count"] == 2


def test_far_report_is_not_duplicate(services, store, tmp_path):
    first = file_one(store, tmp_path, Ctx("s1"), gps=LAW_GARDEN)
    second = file_one(store, tmp_path, Ctx("s2"), gps=CG_ROAD)
    assert second["duplicate"] is False
    assert second["ticket_id"] != first["ticket_id"]


def test_duplicate_by_same_typed_area(services, store, tmp_path):
    first = file_one(store, tmp_path, Ctx("s1"), gps=None, area="CG Road")
    second = file_one(store, tmp_path, Ctx("s2"), gps=None, area="cg road")
    assert second["duplicate"] is True and second["ticket_id"] == first["ticket_id"]


def test_ticket_ids_are_random():
    ids = {tools.new_ticket_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(tools.TICKET_RE.match(i) for i in ids)


def test_status_for_unknown_and_malformed_ids(services):
    ctx = Ctx("s1")
    assert "error" in tools.get_complaint_status("", ctx)
    assert "error" in tools.get_complaint_status("AMC-ZZZZZZ", ctx)
    assert "error" in tools.get_complaint_status("1 OR 1=1", ctx)


def test_social_post(services, store, tmp_path):
    ctx = Ctx("s1")
    res = file_one(store, tmp_path, ctx)
    post = tools.draft_social_post(res["ticket_id"], ctx)
    assert res["ticket_id"] in post["text"]
    assert "@AmdavadAMC" in post["text"] and "#Navrangpura" in post["text"]
    assert post["x_url"].startswith("https://twitter.com/intent/tweet?text=")
    # never leak coordinates into a public post
    assert "23.02" not in post["text"] and "72.56" not in post["text"]


def test_data_files_are_consistent():
    wards = json.loads((tools.DATA_DIR / "wards.json").read_text())
    names = [w["ward"] for w in wards]
    assert len(names) == len(set(names))
    aliases = [a for w in wards for a in w["aliases"]]
    assert len(aliases) == len(set(aliases)), "an alias points to two wards"

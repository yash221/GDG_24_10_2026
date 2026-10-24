from .conftest import make_photo, report

LAW_GARDEN = (23.0265, 72.5605)


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_home_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_report_with_gps_files_ticket(client, session_id):
    r = report(client, session_id, make_photo(gps=LAW_GARDEN))
    assert r.status_code == 200
    body = r.json()
    t = body["ticket"]
    assert t["ward"] == "Navrangpura" and t["department"] == "Roads & Engineering"
    assert t["status"] == "Filed"
    assert [s["tool"] for s in body["steps"]] == [
        "analyze_photo", "get_photo_location", "find_ward_by_coordinates",
        "get_department", "file_complaint",
    ]
    assert t["social"]["text"]


def test_report_with_typed_area(client, session_id):
    r = report(client, session_id, make_photo(), area="near Kankaria Lake")
    assert r.status_code == 200
    assert r.json()["ticket"]["ward"] == "Maninagar"


def test_device_location_used_when_photo_has_none(client, session_id):
    r = client.post("/api/report", data={"session_id": session_id, "lat": "22.9965", "lng": "72.6025"},
                    files={"photo": ("x.jpg", make_photo(), "image/jpeg")})
    assert r.json()["ticket"]["ward"] == "Maninagar"


def test_photo_gps_beats_device_location(client, session_id):
    r = client.post("/api/report", data={"session_id": session_id, "lat": "22.9965", "lng": "72.6025"},
                    files={"photo": ("x.jpg", make_photo(gps=LAW_GARDEN), "image/jpeg")})
    assert r.json()["ticket"]["ward"] == "Navrangpura"


def test_out_of_range_device_location_rejected(client, session_id):
    r = client.post("/api/report", data={"session_id": session_id, "lat": "123", "lng": "72.6"},
                    files={"photo": ("x.jpg", make_photo(), "image/jpeg")})
    assert r.status_code == 422


def test_report_without_location_asks_for_area(client, session_id):
    r = report(client, session_id, make_photo())
    assert r.status_code == 200
    assert r.json()["ticket"] is None


def test_status_question_after_filing(client, session_id):
    tid = report(client, session_id, make_photo(gps=LAW_GARDEN)).json()["ticket"]["id"]
    r = client.post("/api/chat", json={"session_id": session_id, "message": "what's the status?"})
    assert r.status_code == 200
    assert r.json()["steps"][0]["result"]["id"] == tid
    assert "Filed" in r.json()["reply"]


def test_public_ticket_lookup(client, session_id):
    tid = report(client, session_id, make_photo(gps=LAW_GARDEN)).json()["ticket"]["id"]
    r = client.get(f"/api/tickets/{tid}")
    assert r.status_code == 200
    assert r.json()["id"] == tid


def test_admin_moves_status_and_user_sees_it(client, session_id):
    tid = report(client, session_id, make_photo(gps=LAW_GARDEN)).json()["ticket"]["id"]
    r = client.post(f"/api/admin/tickets/{tid}/status", json={"status": "In Progress", "note": "Team sent"},
                    headers={"X-Admin-Token": "test-admin-token-123"})
    assert r.status_code == 200
    r = client.post("/api/chat", json={"session_id": session_id, "message": f"status of {tid}?"})
    assert "In Progress" in r.json()["reply"]
    history = client.get(f"/api/tickets/{tid}").json()["history"]
    assert [h["status"] for h in history] == ["Filed", "In Progress"]


def test_unknown_ticket(client):
    assert client.get("/api/tickets/AMC-ZZZZZZ").status_code == 404


def test_png_and_webp_accepted(client, session_id):
    for fmt in ("PNG", "WEBP"):
        r = report(client, session_id, make_photo(fmt=fmt), area="Paldi", filename=f"x.{fmt.lower()}")
        assert r.status_code == 200, fmt


def test_follow_up_chat_reminds_agent_of_pending_photo(app_env, session_id):
    client, store, vision, agent = app_env
    report(client, session_id, make_photo())  # no location, nothing filed
    pending = store.get_pending_image(session_id)
    assert pending
    client.post("/api/chat", json={"session_id": session_id, "message": "It's near CG Road"})
    assert f"image_id: {pending}" in agent.messages[-1]


def test_no_pending_photo_after_filing(app_env, session_id):
    client, store, *_ = app_env
    report(client, session_id, make_photo(gps=LAW_GARDEN))
    assert store.get_pending_image(session_id) is None


def test_description_is_saved_and_shown_only_to_reporter(app_env, session_id):
    client, store, vision, agent = app_env
    note = "Deep pothole near the bus stop, bikes keep slipping"
    r = client.post("/api/report", data={"session_id": session_id, "area_text": "CG Road", "description": note},
                    files={"photo": ("x.jpg", make_photo(), "image/jpeg")})
    t = r.json()["ticket"]
    assert t["ward"] == "Navrangpura"          # area still parsed correctly
    assert t["user_note"] == note              # reporter sees it
    assert note in agent.messages[-1]          # agent got it as context
    public = client.get(f"/api/tickets/{t['id']}").json()
    assert "user_note" not in public           # strangers don't
    assert note not in t["social"]["text"]     # never in public posts


def test_description_length_limit(client, session_id):
    r = client.post("/api/report", data={"session_id": session_id, "description": "a" * 301},
                    files={"photo": ("x.jpg", make_photo(), "image/jpeg")})
    assert r.status_code == 422


def test_description_whitespace_is_normalised(app_env, session_id):
    client, store, *_ = app_env
    r = client.post("/api/report", data={"session_id": session_id, "area_text": "Paldi", "description": "  big \n\n hole  "},
                    files={"photo": ("x.jpg", make_photo(), "image/jpeg")})
    assert r.json()["ticket"]["user_note"] == "big hole"

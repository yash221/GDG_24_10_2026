import io
import random
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import tools
from app.config import settings
from app.main import create_app
from app.store import SqliteStore


def _rational(deg: float):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((deg - d) * 60 - m) * 60, 4)
    return (float(d), float(m), float(s))


def make_photo(gps=None, fmt="JPEG", size=(640, 480), color=(90, 90, 90), *, seed=None,
               camera=True, taken=None, software=None) -> bytes:
    """Test photo. With a seed it gets a random texture, so its image hash is unique."""
    if seed is None:
        img = Image.new("RGB", size, color)
    else:
        rng = random.Random(seed)
        small = Image.new("RGB", (16, 12))
        small.putdata([tuple(rng.randrange(256) for _ in range(3)) for _ in range(16 * 12)])
        img = small.resize(size, Image.Resampling.BILINEAR)
    exif = Image.Exif()
    if camera:
        exif[0x010F] = "TestPhone"  # Make
    if software:
        exif[0x0131] = software
    if taken:
        exif[0x8769] = {0x9003: taken.strftime("%Y:%m:%d %H:%M:%S")}
    if gps:
        lat, lng = gps
        exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: _rational(abs(lat)),
                        3: "E" if lng >= 0 else "W", 4: _rational(abs(lng))}
    buf = io.BytesIO()
    img.save(buf, fmt, exif=exif)
    return buf.getvalue()


class FakeVision:
    def __init__(self):
        self.result = {"category": "pothole", "severity": "high",
                       "description": "Large pothole in the middle of the road", "confidence": 0.93,
                       "source": "camera_photo"}
        self.calls = 0

    def analyze(self, image_bytes: bytes) -> dict:
        self.calls += 1
        return dict(self.result)


class Ctx:
    """Stand-in for ADK's ToolContext: the tools only use user_id and state."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.state = {}


class FakeAgent:
    """Runs the same tool sequence the real agent is instructed to follow."""

    def __init__(self):
        self.contexts: dict[str, Ctx] = {}
        self.messages: list[str] = []

    async def run(self, session_id: str, text: str) -> dict:
        self.messages.append(text)
        ctx = self.contexts.setdefault(session_id, Ctx(session_id))
        steps = []

        def call(fn, **kwargs):
            result = fn(**kwargs, **({"tool_context": ctx} if "tool_context" in fn.__code__.co_varnames else {}))
            steps.append({"tool": fn.__name__, "args": kwargs, "result": result})
            return result

        m = re.search(r"image_id: ([0-9a-f]+)", text)
        if not m:
            if "status" in text.lower() or "update" in text.lower():
                tid = re.search(r"AMC-[A-Z0-9]{6}", text.upper())
                res = call(tools.get_complaint_status, ticket_id=tid.group(0) if tid else "")
                return {"reply": f"Status: {res.get('status', res.get('error'))}", "steps": steps}
            return {"reply": "I can only help with civic complaints.", "steps": steps}

        image_id = m.group(1)
        analysis = call(tools.analyze_photo, image_id=image_id)
        if analysis.get("category") == "not_civic" or analysis.get("confidence", 0) < 0.6:
            return {"reply": "Not sure what this is. Can you confirm the issue?", "steps": steps}
        loc = call(tools.get_photo_location, image_id=image_id)
        area = re.search(r'Area I typed: "(.*?)"', text)
        area = area.group(1) if area else ""
        if loc.get("has_location"):
            ward = call(tools.find_ward_by_coordinates, latitude=loc["latitude"], longitude=loc["longitude"])
        elif area:
            ward = call(tools.find_ward_by_area, area_name=area)
        else:
            return {"reply": "Which area is this in?", "steps": steps}
        if not ward.get("found"):
            return {"reply": "I couldn't find that ward.", "steps": steps}
        call(tools.get_department, category=analysis["category"])
        res = call(tools.file_complaint, image_id=image_id, ward=ward["ward"],
                   area_name=area, confirmed_category="")
        return {"reply": f"Filed {res.get('ticket_id')}", "steps": steps}


@pytest.fixture
def store(tmp_path):
    return SqliteStore(str(tmp_path / "test.db"))


@pytest.fixture
def vision():
    return FakeVision()


@pytest.fixture
def services(store, vision):
    s = tools.Services(store=store, vision=vision)
    tools.configure(s)
    return s


@pytest.fixture
def app_env(tmp_path, monkeypatch, store, vision):
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "admin_token", "test-admin-token-123")
    agent = FakeAgent()
    app = create_app(store=store, vision=vision, agent=agent)
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, vision, agent


@pytest.fixture
def client(app_env):
    return app_env[0]


@pytest.fixture
def session_id(client):
    return client.post("/api/session").json()["session_id"]


def report(client, session_id, photo: bytes, area: str = "", filename="road.jpg"):
    return client.post(
        "/api/report",
        data={"session_id": session_id, "area_text": area},
        files={"photo": (filename, photo, "image/jpeg")},
    )

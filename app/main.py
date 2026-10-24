import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import geo, rewards, tools
from .config import BASE_DIR, settings
from .images import BadImage, process_upload
from .store import STATUSES, make_store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("civic")

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


class RateLimiter:
    """Sliding-window limiter kept in memory.

    Fine for one instance; behind a load balancer this would move to
    Redis (Memorystore) without changing the callers.
    """

    def __init__(self, max_keys: int = 50_000):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window: int = 3600) -> bool:
        t = time.monotonic()
        if len(self._hits) > self._max_keys:
            self._prune(t - window)
        q = self._hits[key]
        while q and q[0] <= t - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True

    def _prune(self, cutoff: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[key]


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # earlier entries can be set by the client; the proxy appends the real one last
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class ChatIn(BaseModel):
    session_id: str = Field(min_length=20, max_length=64)
    message: str = Field(min_length=1, max_length=500)


class SessionIn(BaseModel):
    session_id: str = Field(min_length=20, max_length=64)


class StatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=200)


def create_app(store=None, vision=None, agent=None) -> FastAPI:
    app = FastAPI(title="Civic Complaint Agent", docs_url=None, redoc_url=None, openapi_url=None)

    store = store or make_store(settings)
    if vision is None:
        from .vision import GeminiVision
        vision = GeminiVision(settings.vision_model)
    tools.configure(tools.Services(store=store, vision=vision, maps_api_key=settings.maps_api_key))
    if agent is None:
        from .agent import ComplaintAgent
        agent = ComplaintAgent(settings.model)

    limiter = RateLimiter()
    app.state.store, app.state.agent, app.state.limiter = store, agent, limiter

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def guard_requests(request: Request, call_next):
        # refuse oversized bodies before the multipart parser spools them to disk
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None and request.url.path == "/api/report":
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
            if length and (not length.isdigit() or int(length) > settings.max_upload_bytes + 64_000):
                return JSONResponse({"detail": "Photo is too large (max 8 MB)"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)

    def require_session(session_id: str) -> str:
        if not SESSION_RE.match(session_id or "") or not store.session_exists(session_id):
            raise HTTPException(401, "Session expired. Refresh the page.")
        return session_id

    def limit(key: str, n: int) -> None:
        if not limiter.allow(key, n):
            raise HTTPException(429, "Too many requests. Please wait a while and try again.")

    async def run_agent(session_id: str, text: str) -> dict:
        try:
            result = await agent.run(session_id, text)
        except Exception:
            log.exception("agent run failed")
            raise HTTPException(503, "The assistant is busy right now. Please try again.")
        ticket = None
        filed = [s for s in result["steps"] if s["tool"] == "file_complaint"
                 and isinstance(s.get("result"), dict) and s["result"].get("ticket_id")]
        if filed:
            t = store.get_ticket(filed[-1]["result"]["ticket_id"])
            if t:
                ticket = {**tools.public_view(t), "social": tools.social_post(t)}
        return {**result, "ticket": ticket, "rewards": rewards.summary(store, session_id)}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/session")
    def new_session(request: Request):
        limit(f"session:{client_ip(request)}", settings.sessions_per_hour_per_ip)
        session_id = secrets.token_urlsafe(24)
        store.create_session(session_id)
        return {"session_id": session_id}

    @app.post("/api/report")
    async def report(session_id: str = Form(...), area_text: str = Form("", max_length=100),
                     lat: float | None = Form(None, ge=-90, le=90),
                     lng: float | None = Form(None, ge=-180, le=180),
                     photo: UploadFile = File(...)):
        require_session(session_id)
        limit(f"report:{session_id}", settings.reports_per_hour)

        data = await photo.read(settings.max_upload_bytes + 1)
        try:
            image = await asyncio.to_thread(process_upload, data, settings.upload_dir,
                                            settings.max_upload_bytes, settings.max_image_side)
        except BadImage as e:
            raise HTTPException(400, str(e))
        # phone browsers often strip GPS from uploads, so the page can send the
        # device location instead; the photo's own GPS wins when both exist
        if image["lat"] is None and lat is not None and lng is not None:
            image["lat"], image["lng"] = round(lat, 6), round(lng, 6)
        elif image["lat"] is not None and lat is not None and lng is not None:
            # photo GPS far from where the phone is now: weak sign of a forwarded photo
            image["meta"]["location_mismatch"] = geo.haversine_km(image["lat"], image["lng"], lat, lng) > 2
        store.save_image({**image, "session_id": session_id})
        store.set_pending_image(session_id, image["id"])

        area = " ".join(area_text.split())
        has_gps = "yes" if image["lat"] is not None else "no"
        msg = (f"I want to report a civic issue. Photo image_id: {image['id']}. "
               f"Location available: {has_gps}. "
               + (f'Area I typed: "{area}"' if area else "I did not type an area."))
        return await run_agent(session_id, msg)

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        require_session(body.session_id)
        limit(f"chat:{body.session_id}", settings.chats_per_hour)
        message = body.message.strip()
        # a follow-up ("it's near CG Road") may reach an instance that never saw
        # the photo, so remind the agent which photo is still waiting to be filed
        pending = store.get_pending_image(body.session_id)
        if pending:
            message = f"(My photo waiting to be filed has image_id: {pending}.)\n{message}"
        return await run_agent(body.session_id, message)

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, request: Request):
        # IDs are random, but cap lookups so nobody can sweep the ID space
        limit(f"lookup:{client_ip(request)}", settings.lookups_per_hour_per_ip)
        ticket_id = ticket_id.upper()
        if not tools.TICKET_RE.match(ticket_id):
            raise HTTPException(400, "Invalid tracking ID")
        t = store.get_ticket(ticket_id)
        if not t:
            raise HTTPException(404, "Complaint not found")
        return tools.public_view(t)

    @app.post("/api/admin/tickets/{ticket_id}/status")
    def set_status(ticket_id: str, body: StatusIn, x_admin_token: str = Header(default="")):
        if not settings.admin_token or not secrets.compare_digest(
                x_admin_token.encode(), settings.admin_token.encode()):
            raise HTTPException(403, "Forbidden")
        if not tools.TICKET_RE.match(ticket_id) or body.status not in STATUSES:
            raise HTTPException(400, "Invalid request")
        t = store.update_status(ticket_id, body.status, body.note.strip())
        if not t:
            raise HTTPException(404, "Complaint not found")
        if body.status == "Resolved":
            # fixed by AMC is the strongest proof the report was real
            rewards.award(store, t["session_id"], t, "resolved")
        return tools.public_view(t)

    @app.post("/api/rewards")
    def my_rewards(body: SessionIn):
        require_session(body.session_id)
        return rewards.summary(store, body.session_id)

    @app.get("/api/leaderboard")
    def leaderboard(request: Request):
        limit(f"lookup:{client_ip(request)}", settings.lookups_per_hour_per_ip)
        return {"leaders": rewards.leaderboard(store)}

    app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
    return app


def get_app() -> FastAPI:
    return create_app()

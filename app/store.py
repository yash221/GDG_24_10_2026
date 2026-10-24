"""Mock AMC complaint system.

Two backends with the same interface: SQLite for local runs and tests,
Firestore when deployed (Cloud Run instances don't keep local files).
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol

STATUSES = ["Filed", "Assigned", "In Progress", "Resolved"]


def now() -> datetime:
    return datetime.now(timezone.utc)


class Store(Protocol):
    def create_session(self, session_id: str) -> None: ...
    def session_exists(self, session_id: str) -> bool: ...
    def set_last_ticket(self, session_id: str, ticket_id: str) -> None: ...
    def get_last_ticket(self, session_id: str) -> str | None: ...
    def save_image(self, image: dict) -> None: ...
    def get_image(self, image_id: str) -> dict | None: ...
    def set_image_analysis(self, image_id: str, analysis: dict) -> None: ...
    def create_ticket(self, ticket: dict) -> None: ...
    def get_ticket(self, ticket_id: str) -> dict | None: ...
    def recent_tickets(self, category: str, since: datetime) -> list[dict]: ...
    def add_report(self, ticket_id: str) -> None: ...
    def update_status(self, ticket_id: str, status: str, note: str) -> dict | None: ...


class SqliteStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, last_ticket_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, path TEXT NOT NULL,
                    lat REAL, lng REAL, analysis TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, image_id TEXT NOT NULL,
                    category TEXT NOT NULL, severity TEXT NOT NULL, description TEXT NOT NULL,
                    ward TEXT NOT NULL, zone TEXT NOT NULL, department TEXT NOT NULL,
                    lat REAL, lng REAL, area_text TEXT, status TEXT NOT NULL,
                    history TEXT NOT NULL, report_count INTEGER NOT NULL DEFAULT 1,
                    expected_by TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_tickets_cat_time ON tickets(category, created_at);
                """
            )

    def _one(self, sql: str, args: tuple) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _exec(self, sql: str, args: tuple) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, args)

    def create_session(self, session_id):
        self._exec("INSERT INTO sessions (id, created_at) VALUES (?, ?)",
                   (session_id, now().isoformat()))

    def session_exists(self, session_id):
        return self._one("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) is not None

    def set_last_ticket(self, session_id, ticket_id):
        self._exec("UPDATE sessions SET last_ticket_id = ? WHERE id = ?", (ticket_id, session_id))

    def get_last_ticket(self, session_id):
        row = self._one("SELECT last_ticket_id FROM sessions WHERE id = ?", (session_id,))
        return row["last_ticket_id"] if row else None

    def save_image(self, image):
        self._exec(
            "INSERT INTO images (id, session_id, path, lat, lng, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (image["id"], image["session_id"], image["path"], image.get("lat"), image.get("lng"),
             now().isoformat()),
        )

    def get_image(self, image_id):
        row = self._one("SELECT * FROM images WHERE id = ?", (image_id,))
        if row and row["analysis"]:
            row["analysis"] = json.loads(row["analysis"])
        return row

    def set_image_analysis(self, image_id, analysis):
        self._exec("UPDATE images SET analysis = ? WHERE id = ?", (json.dumps(analysis), image_id))

    def create_ticket(self, ticket):
        self._exec(
            """INSERT INTO tickets (id, session_id, image_id, category, severity, description,
               ward, zone, department, lat, lng, area_text, status, history, report_count,
               expected_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket["id"], ticket["session_id"], ticket["image_id"], ticket["category"],
             ticket["severity"], ticket["description"], ticket["ward"], ticket["zone"],
             ticket["department"], ticket.get("lat"), ticket.get("lng"), ticket.get("area_text"),
             ticket["status"], json.dumps(ticket["history"]), ticket.get("report_count", 1),
             ticket["expected_by"], ticket["created_at"]),
        )

    def get_ticket(self, ticket_id):
        row = self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if row:
            row["history"] = json.loads(row["history"])
        return row

    def recent_tickets(self, category, since):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE category = ? AND created_at >= ?",
                (category, since.isoformat()),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["history"] = json.loads(d["history"])
            out.append(d)
        return out

    def add_report(self, ticket_id):
        self._exec("UPDATE tickets SET report_count = report_count + 1 WHERE id = ?", (ticket_id,))

    def update_status(self, ticket_id, status, note):
        with self._lock, self._conn:
            row = self._conn.execute("SELECT history FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                return None
            history = json.loads(row["history"])
            history.append({"status": status, "at": now().isoformat(), "note": note})
            self._conn.execute("UPDATE tickets SET status = ?, history = ? WHERE id = ?",
                               (status, json.dumps(history), ticket_id))
        return self.get_ticket(ticket_id)


class FirestoreStore:
    def __init__(self, project: str | None = None):
        from google.cloud import firestore

        self._fs = firestore
        self._db = firestore.Client(project=project or None)

    def _doc(self, collection: str, doc_id: str):
        return self._db.collection(collection).document(doc_id)

    def _get(self, collection: str, doc_id: str) -> dict | None:
        snap = self._doc(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None

    def create_session(self, session_id):
        self._doc("sessions", session_id).set({"id": session_id, "created_at": now().isoformat()})

    def session_exists(self, session_id):
        return self._doc("sessions", session_id).get().exists

    def set_last_ticket(self, session_id, ticket_id):
        self._doc("sessions", session_id).update({"last_ticket_id": ticket_id})

    def get_last_ticket(self, session_id):
        data = self._get("sessions", session_id)
        return data.get("last_ticket_id") if data else None

    def save_image(self, image):
        self._doc("images", image["id"]).set({**image, "analysis": None, "created_at": now().isoformat()})

    def get_image(self, image_id):
        return self._get("images", image_id)

    def set_image_analysis(self, image_id, analysis):
        self._doc("images", image_id).update({"analysis": analysis})

    def create_ticket(self, ticket):
        self._doc("tickets", ticket["id"]).set({"report_count": 1, **ticket})

    def get_ticket(self, ticket_id):
        return self._get("tickets", ticket_id)

    def recent_tickets(self, category, since):
        # single-field range query, so no composite index is needed
        query = self._db.collection("tickets").where(
            filter=self._fs.FieldFilter("created_at", ">=", since.isoformat())
        )
        return [d.to_dict() for d in query.stream() if d.get("category") == category]

    def add_report(self, ticket_id):
        self._doc("tickets", ticket_id).update({"report_count": self._fs.Increment(1)})

    def update_status(self, ticket_id, status, note):
        ref = self._doc("tickets", ticket_id)
        if not ref.get().exists:
            return None
        entry = {"status": status, "at": now().isoformat(), "note": note}
        ref.update({"status": status, "history": self._fs.ArrayUnion([entry])})
        return self.get_ticket(ticket_id)


def make_store(settings) -> Store:
    if settings.store == "firestore":
        return FirestoreStore(settings.gcp_project)
    return SqliteStore(settings.db_path)


def expected_by(sla_days: int) -> str:
    return (now() + timedelta(days=sla_days)).date().isoformat()

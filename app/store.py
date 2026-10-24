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
    def set_pending_image(self, session_id: str, image_id: str | None) -> None: ...
    def get_pending_image(self, session_id: str) -> str | None: ...
    def add_resolved_ward(self, session_id: str, ward: str) -> None: ...
    def get_resolved_wards(self, session_id: str) -> list[str]: ...
    def save_image(self, image: dict) -> None: ...
    def get_image(self, image_id: str) -> dict | None: ...
    def set_image_analysis(self, image_id: str, analysis: dict) -> None: ...
    def create_ticket(self, ticket: dict) -> None: ...
    def get_ticket(self, ticket_id: str) -> dict | None: ...
    def recent_tickets(self, category: str, since: datetime) -> list[dict]: ...
    def add_report(self, ticket_id: str) -> None: ...
    def update_status(self, ticket_id: str, status: str, note: str) -> dict | None: ...
    def recent_image_hashes(self, since: datetime) -> list[dict]: ...
    def add_reward(self, entry: dict) -> None: ...
    def rewards_for(self, session_id: str) -> list[dict]: ...
    def has_reward(self, session_id: str, ticket_id: str, reason: str) -> bool: ...
    def top_reporters(self, limit: int) -> list[tuple[str, int]]: ...


class SqliteStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, last_ticket_id TEXT, pending_image_id TEXT,
                    resolved_wards TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, path TEXT NOT NULL,
                    lat REAL, lng REAL, analysis TEXT, meta TEXT, phash TEXT, note TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, image_id TEXT NOT NULL,
                    category TEXT NOT NULL, severity TEXT NOT NULL, description TEXT NOT NULL,
                    ward TEXT NOT NULL, zone TEXT NOT NULL, department TEXT NOT NULL,
                    lat REAL, lng REAL, area_text TEXT, status TEXT NOT NULL,
                    history TEXT NOT NULL, report_count INTEGER NOT NULL DEFAULT 1,
                    expected_by TEXT NOT NULL, created_at TEXT NOT NULL,
                    trust_score INTEGER, trust_level TEXT, trust_reasons TEXT, user_note TEXT);
                CREATE TABLE IF NOT EXISTS rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL, reason TEXT NOT NULL, category TEXT NOT NULL,
                    points INTEGER NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE (session_id, ticket_id, reason));
                CREATE INDEX IF NOT EXISTS idx_images_time ON images(created_at);
                CREATE INDEX IF NOT EXISTS idx_tickets_cat_time ON tickets(category, created_at);
                """
            )
            for table, column, kind in (("images", "meta", "TEXT"), ("images", "phash", "TEXT"),
                                        ("images", "note", "TEXT"), ("tickets", "user_note", "TEXT"),
                                        ("tickets", "trust_score", "INTEGER"),
                                        ("tickets", "trust_level", "TEXT"),
                                        ("tickets", "trust_reasons", "TEXT")):
                existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            if "pending_image_id" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN pending_image_id TEXT")
            if "resolved_wards" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN resolved_wards TEXT NOT NULL DEFAULT '[]'")

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

    def set_pending_image(self, session_id, image_id):
        self._exec("UPDATE sessions SET pending_image_id = ? WHERE id = ?", (image_id, session_id))

    def get_pending_image(self, session_id):
        row = self._one("SELECT pending_image_id FROM sessions WHERE id = ?", (session_id,))
        return row["pending_image_id"] if row else None

    def add_resolved_ward(self, session_id, ward):
        with self._lock, self._conn:
            row = self._conn.execute("SELECT resolved_wards FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return
            wards = json.loads(row["resolved_wards"])
            if ward not in wards:
                wards.append(ward)
                self._conn.execute("UPDATE sessions SET resolved_wards = ? WHERE id = ?",
                                   (json.dumps(wards), session_id))

    def get_resolved_wards(self, session_id):
        row = self._one("SELECT resolved_wards FROM sessions WHERE id = ?", (session_id,))
        return json.loads(row["resolved_wards"]) if row else []

    def save_image(self, image):
        self._exec(
            "INSERT INTO images (id, session_id, path, lat, lng, meta, phash, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (image["id"], image["session_id"], image["path"], image.get("lat"), image.get("lng"),
             json.dumps(image.get("meta") or {}), (image.get("meta") or {}).get("phash"),
             image.get("note"), now().isoformat()),
        )

    def get_image(self, image_id):
        row = self._one("SELECT * FROM images WHERE id = ?", (image_id,))
        if row:
            row["analysis"] = json.loads(row["analysis"]) if row["analysis"] else None
            row["meta"] = json.loads(row["meta"]) if row.get("meta") else {}
        return row

    def set_image_analysis(self, image_id, analysis):
        self._exec("UPDATE images SET analysis = ? WHERE id = ?", (json.dumps(analysis), image_id))

    def create_ticket(self, ticket):
        self._exec(
            """INSERT INTO tickets (id, session_id, image_id, category, severity, description,
               ward, zone, department, lat, lng, area_text, status, history, report_count,
               expected_by, created_at, trust_score, trust_level, trust_reasons, user_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket["id"], ticket["session_id"], ticket["image_id"], ticket["category"],
             ticket["severity"], ticket["description"], ticket["ward"], ticket["zone"],
             ticket["department"], ticket.get("lat"), ticket.get("lng"), ticket.get("area_text"),
             ticket["status"], json.dumps(ticket["history"]), ticket.get("report_count", 1),
             ticket["expected_by"], ticket["created_at"], ticket.get("trust_score"),
             ticket.get("trust_level"), json.dumps(ticket.get("trust_reasons") or []),
             ticket.get("user_note")),
        )

    def get_ticket(self, ticket_id):
        row = self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return _decode_ticket(row) if row else None

    def recent_tickets(self, category, since):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE category = ? AND created_at >= ?",
                (category, since.isoformat()),
            ).fetchall()
        return [_decode_ticket(dict(r)) for r in rows]

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

    def recent_image_hashes(self, since):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, phash FROM images WHERE created_at >= ? AND phash IS NOT NULL",
                (since.isoformat(),)).fetchall()
        return [dict(r) for r in rows]

    def add_reward(self, entry):
        self._exec(
            "INSERT OR IGNORE INTO rewards (session_id, ticket_id, reason, category, points, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry["session_id"], entry["ticket_id"], entry["reason"], entry["category"],
             entry["points"], entry["created_at"]))

    def rewards_for(self, session_id):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM rewards WHERE session_id = ?", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def has_reward(self, session_id, ticket_id, reason):
        return self._one("SELECT 1 FROM rewards WHERE session_id = ? AND ticket_id = ? AND reason = ?",
                         (session_id, ticket_id, reason)) is not None

    def top_reporters(self, limit):
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, SUM(points) AS total FROM rewards GROUP BY session_id "
                "ORDER BY total DESC LIMIT ?", (limit,)).fetchall()
        return [(r["session_id"], r["total"]) for r in rows]


class FirestoreStore:
    def __init__(self, project: str | None = None, credentials_info: dict | None = None):
        from google.cloud import firestore

        self._fs = firestore
        creds = None
        if credentials_info:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(credentials_info)
            project = project or credentials_info.get("project_id")
        self._db = firestore.Client(project=project or None, credentials=creds)

    def _doc(self, collection: str, doc_id: str):
        return self._db.collection(collection).document(doc_id)

    def _get(self, collection: str, doc_id: str) -> dict | None:
        snap = self._doc(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None

    def create_session(self, session_id):
        self._doc("sessions", session_id).set(
            {"id": session_id, "resolved_wards": [], "created_at": now().isoformat()})

    def session_exists(self, session_id):
        return self._doc("sessions", session_id).get().exists

    def set_last_ticket(self, session_id, ticket_id):
        self._doc("sessions", session_id).update({"last_ticket_id": ticket_id})

    def get_last_ticket(self, session_id):
        data = self._get("sessions", session_id)
        return data.get("last_ticket_id") if data else None

    def set_pending_image(self, session_id, image_id):
        self._doc("sessions", session_id).update({"pending_image_id": image_id})

    def get_pending_image(self, session_id):
        data = self._get("sessions", session_id)
        return data.get("pending_image_id") if data else None

    def add_resolved_ward(self, session_id, ward):
        self._doc("sessions", session_id).update({"resolved_wards": self._fs.ArrayUnion([ward])})

    def get_resolved_wards(self, session_id):
        data = self._get("sessions", session_id)
        return list(data.get("resolved_wards", [])) if data else []

    def save_image(self, image):
        self._doc("images", image["id"]).set({**image, "analysis": None,
                                               "phash": (image.get("meta") or {}).get("phash"),
                                               "created_at": now().isoformat()})

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

    def recent_image_hashes(self, since):
        query = self._db.collection("images").where(
            filter=self._fs.FieldFilter("created_at", ">=", since.isoformat()))
        return [{"id": d.id, "session_id": d.get("session_id"), "phash": d.get("phash")}
                for d in query.stream() if d.get("phash")]

    def add_reward(self, entry):
        # one document per (session, ticket, reason) makes awards idempotent
        key = f"{entry['session_id']}:{entry['ticket_id']}:{entry['reason']}"
        self._doc("rewards", key).set(entry)

    def rewards_for(self, session_id):
        query = self._db.collection("rewards").where(
            filter=self._fs.FieldFilter("session_id", "==", session_id))
        return [d.to_dict() for d in query.stream()]

    def has_reward(self, session_id, ticket_id, reason):
        return self._doc("rewards", f"{session_id}:{ticket_id}:{reason}").get().exists

    def top_reporters(self, limit):
        totals: dict[str, int] = {}
        for d in self._db.collection("rewards").stream():
            totals[d.get("session_id")] = totals.get(d.get("session_id"), 0) + d.get("points")
        return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def _decode_ticket(row: dict) -> dict:
    row["history"] = json.loads(row["history"])
    row["trust_reasons"] = json.loads(row["trust_reasons"]) if row.get("trust_reasons") else []
    return row




def make_store(settings) -> Store:
    if settings.store == "firestore":
        return FirestoreStore(settings.gcp_project, settings.credentials_info())
    return SqliteStore(settings.db_path)


def expected_by(sla_days: int) -> str:
    return (now() + timedelta(days=sla_days)).date().isoformat()

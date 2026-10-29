"""Local SQLite cache: users, check-ins (with sync state) and key/values."""
import json
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    descriptor  TEXT,               -- JSON list of 128 floats (face-api.js, from the main server; unused)
    has_photo   INTEGER NOT NULL DEFAULT 0,
    photo_file  TEXT,               -- local cached file name
    active      INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT,
    embedding   TEXT,               -- JSON list of 128 floats computed here from the photo (SFace)
    embedding_src TEXT              -- photo version the embedding was computed from
);
CREATE TABLE IF NOT EXISTS checkins (
    uuid          TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    direction     TEXT NOT NULL,     -- IN / OUT
    ts            TEXT NOT NULL,     -- ISO 8601 with UTC offset (local time)
    status        TEXT NOT NULL,     -- OK / KO
    reason        TEXT,
    distance      REAL,
    snapshot_file TEXT,
    timezone      TEXT,              -- site time zone at check time (e.g. Indian/Antananarivo)
    relay_name    TEXT,              -- relay (site) that made the check
    sync_state    TEXT NOT NULL DEFAULT 'pending',  -- pending / synced / rejected
    sync_error    TEXT,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkins_ts ON checkins(ts);
CREATE INDEX IF NOT EXISTS idx_checkins_sync ON checkins(sync_state);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Database:
    def __init__(self, path: Path, tz=None, photos_dir: Path | None = None):
        self.path = str(path)
        self.photos_dir = Path(photos_dir) if photos_dir else Path(path).parent / "photos"
        self.tz = tz  # site time zone, defines what "a day" is
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after the first release (existing databases)."""
        c = self.conn()
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for name in ("embedding", "embedding_src"):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(checkins)")}
        for name in ("timezone", "relay_name"):
            if name not in cols:
                c.execute(f"ALTER TABLE checkins ADD COLUMN {name} TEXT")
                if name == "relay_name":
                    # existing rows: this relay's site name (time zone was never recorded)
                    c.execute("UPDATE checkins SET relay_name=? WHERE relay_name IS NULL",
                              (self.get("site_name", "") or None,))
        c.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def write(self, sql, params=()):
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, params)
            c.commit()
            return cur

    # ------------------------------------------------------------------ kv
    def get(self, key, default=None):
        row = self.conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.write("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # --------------------------------------------------------------- users
    def users(self, active_only=True):
        sql = "SELECT * FROM users" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self.conn().execute(sql)]

    def user(self, user_id):
        row = self.conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def upsert_user(self, u: dict, photo_file):
        self.write(
            """INSERT INTO users(id, full_name, descriptor, has_photo, photo_file, active, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, descriptor=excluded.descriptor,
                 has_photo=excluded.has_photo, photo_file=excluded.photo_file, active=excluded.active,
                 updated_at=excluded.updated_at""",
            (u["id"], u["full_name"], json.dumps(u["descriptor"]) if u.get("descriptor") else None,
             int(bool(u.get("has_photo"))), photo_file, int(bool(u.get("active", True))), u.get("updated_at")),
        )

    @staticmethod
    def photo_version(u):
        return f"{u['photo_file']}|{u['updated_at']}" if u["photo_file"] else None

    def photo_path(self, u):
        return self.photos_dir / u["photo_file"]

    @staticmethod
    def embedding(u):
        return json.loads(u["embedding"]) if u.get("embedding") else None

    def users_needing_embedding(self):
        """Active users whose embedding is missing or older than their photo (or photo removed)."""
        return [u for u in self.users() if (u["embedding_src"] or None) != self.photo_version(u)]

    def set_embedding(self, user_id, embedding, src):
        self.write("UPDATE users SET embedding=?, embedding_src=? WHERE id=?",
                   (json.dumps(embedding) if embedding is not None else None, src, user_id))

    def deactivate_missing_users(self, ids):
        known = {r["id"] for r in self.conn().execute("SELECT id FROM users")}
        for uid in known - set(ids):
            self.write("UPDATE users SET active=0 WHERE id=?", (uid,))

    # ------------------------------------------------------------ checkins
    def add_checkin(self, row: dict):
        cols = ",".join(row)
        self.write(f"INSERT INTO checkins({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def _day_range(self, day: date):
        if self.tz is not None:
            start = datetime.combine(day, time.min, tzinfo=self.tz)
        else:
            start = datetime.combine(day, time.min).astimezone()
        return start.isoformat(), (start + timedelta(days=1)).isoformat()

    # ts values carry their UTC offset: compare them as instants, not as text
    def checkins_for_day(self, day: date):
        a, b = self._day_range(day)
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM checkins WHERE julianday(ts) >= julianday(?) AND julianday(ts) < julianday(?) "
            "ORDER BY julianday(ts) DESC", (a, b))]

    def last_checkin(self, user_id, day: date):
        a, b = self._day_range(day)
        row = self.conn().execute(
            "SELECT * FROM checkins WHERE user_id=? AND julianday(ts) >= julianday(?) AND julianday(ts) < julianday(?) "
            "ORDER BY julianday(ts) DESC LIMIT 1", (user_id, a, b)).fetchone()
        return dict(row) if row else None

    def pending_checkins(self, limit=20):
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM checkins WHERE sync_state='pending' ORDER BY ts LIMIT ?", (limit,))]

    def mark_synced(self, uuids, when):
        for u in uuids:
            self.write("UPDATE checkins SET sync_state='synced', sync_error=NULL, synced_at=? WHERE uuid=?", (when, u))

    def mark_rejected(self, uuid, error):
        self.write("UPDATE checkins SET sync_state='rejected', sync_error=? WHERE uuid=?", (error, uuid))

    def retry_rejected(self):
        self.write("UPDATE checkins SET sync_state='pending', sync_error=NULL WHERE sync_state='rejected'")

    def counts(self):
        rows = self.conn().execute("SELECT sync_state, COUNT(*) n FROM checkins GROUP BY sync_state").fetchall()
        return {r["sync_state"]: r["n"] for r in rows}

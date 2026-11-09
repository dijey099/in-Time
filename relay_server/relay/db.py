"""Local SQLite cache: users, check-ins (with sync state) and key/values, encrypted.

Everything that matters is sealed with the relay's data key (AES-256-GCM, see crypto.py) and bound
to its row: names, face data, photos, check-ins, settings. Only the relay can read or write it;
a value changed, copied or moved outside the relay fails to decrypt and is ignored. Plain columns
are only indexes (pseudonymous user tag, day, sequence number) and the sync state, and they are
checked against the sealed record whenever a row is read.

Each check-in also carries a sequence number and the relay's signature (made when the check-in is
created, with the trusted time): the main server rejects anything not signed by this relay and
flags missing numbers (check-ins deleted here).
"""
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from .crypto import Tampered

log = logging.getLogger("relay.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv2 (key TEXT PRIMARY KEY, sealed BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS users2 (tag TEXT PRIMARY KEY, sealed BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS checkins2 (
    uuid        TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    day         TEXT NOT NULL,              -- local date (site time zone) of the check-in
    user_tag    TEXT NOT NULL,              -- pseudonym of the user ID (Vault.tag)
    sealed      BLOB NOT NULL,              -- the check-in record, signed by the relay
    sync_state  TEXT NOT NULL DEFAULT 'pending',  -- pending / synced / rejected / tampered
    sync_error  TEXT,
    synced_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkins2_day ON checkins2(day, seq);
CREATE INDEX IF NOT EXISTS idx_checkins2_user ON checkins2(user_tag, day, seq);
CREATE INDEX IF NOT EXISTS idx_checkins2_sync ON checkins2(sync_state, seq);
"""

_CHECK = "vault_check"


class DataKeyMismatch(Exception):
    """relay.db was encrypted with another data key (secrets.bin deleted or from another relay)."""


class Database:
    def __init__(self, path: Path, tz=None, photos_dir: Path | None = None, snapshots_dir: Path | None = None,
                 vault=None, identity=None, relay_id: str = ""):
        self.path = str(path)
        self.photos_dir = Path(photos_dir) if photos_dir else Path(path).parent / "photos"
        self.snapshots_dir = Path(snapshots_dir) if snapshots_dir else Path(path).parent / "snapshots"
        self.tz = tz  # site time zone, defines what "a day" is
        self.vault, self.identity, self.relay_id = vault, identity, relay_id
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._kv_cache, self._users_cache, self._checkin_cache = {}, None, {}
        self._warned = set()
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()
        self._check_key()
        self._migrate_v1()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA secure_delete=ON")  # removed data is overwritten, not left in the file
            self._local.conn = c
        return c

    def write(self, sql, params=()):
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, params)
            c.commit()
            return cur

    def _tampered(self, what):
        if what not in self._warned:
            self._warned.add(what)
            log.error("Ignored %s: it was changed outside the relay (or can't be decrypted)", what)

    def _check_key(self):
        row = self.conn().execute("SELECT sealed FROM kv2 WHERE key=?", (_CHECK,)).fetchone()
        if row is None:
            if self.conn().execute("SELECT 1 FROM checkins2 UNION ALL SELECT 1 FROM users2 LIMIT 1").fetchone():
                raise DataKeyMismatch("encrypted data without key check")
            self.write("INSERT INTO kv2(key, sealed) VALUES(?, ?)", (_CHECK, self.vault.seal(b"ok", "kv|" + _CHECK)))
            return
        try:
            self.vault.open(row["sealed"], "kv|" + _CHECK)
        except Tampered:
            raise DataKeyMismatch("relay.db was encrypted with another key") from None

    # ------------------------------------------------------------------ kv
    def get(self, key, default=None):
        if key in self._kv_cache:
            return self._kv_cache[key]
        row = self.conn().execute("SELECT sealed FROM kv2 WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            value = self.vault.open_json(row["sealed"], "kv|" + key)
        except Tampered:
            self._tampered(f"setting {key!r}")
            return default
        self._kv_cache[key] = value
        return value

    def set(self, key, value):
        with self._write_lock:
            self.write("INSERT INTO kv2(key, sealed) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET sealed=excluded.sealed",
                       (key, self.vault.seal_json(value, "kv|" + key)))
            self._kv_cache[key] = value

    # --------------------------------------------------------------- users
    def _all_users(self):
        if self._users_cache is None:
            cache = {}
            for row in self.conn().execute("SELECT tag, sealed FROM users2"):
                try:
                    u = self.vault.open_json(row["sealed"], "user|" + row["tag"])
                    if self.vault.tag(u["id"]) != row["tag"]:
                        raise Tampered(row["tag"])
                except (Tampered, KeyError, ValueError):
                    self._tampered(f"user record {row['tag'][:8]}")
                    continue
                cache[u["id"]] = u
            self._users_cache = cache
        return self._users_cache

    def _save_user(self, u):
        tag = self.vault.tag(u["id"])
        with self._write_lock:
            self.write("INSERT INTO users2(tag, sealed) VALUES(?, ?) ON CONFLICT(tag) DO UPDATE SET sealed=excluded.sealed",
                       (tag, self.vault.seal_json(u, "user|" + tag)))
            self._all_users()[u["id"]] = u

    def users(self, active_only=True):
        return [dict(u) for _, u in sorted(self._all_users().items()) if u.get("active") or not active_only]

    def user(self, user_id):
        u = self._all_users().get(str(user_id))
        return dict(u) if u else None

    def upsert_user(self, u: dict, photo_file, photo_ext=None):
        with self._write_lock:
            old = self._all_users().get(str(u["id"])) or {}
            rec = {**old, "id": str(u["id"]), "full_name": u["full_name"], "descriptor": u.get("descriptor"),
                   "has_photo": bool(u.get("has_photo")), "photo_file": photo_file,
                   "photo_ext": photo_ext or old.get("photo_ext"), "active": bool(u.get("active", True)),
                   "updated_at": u.get("updated_at")}
            rec.setdefault("embedding", None)
            rec.setdefault("embedding_src", None)
            self._save_user(rec)

    @staticmethod
    def photo_version(u):
        return f"{u['photo_file']}|{u['updated_at']}" if u.get("photo_file") else None

    def save_photo(self, user_id, data: bytes, ext: str) -> str:
        """Store a user's photo encrypted; returns its file name."""
        tag = self.vault.tag(user_id)
        name = f"{tag}.bin"
        tmp = self.photos_dir / (name + ".tmp")
        tmp.write_bytes(self.vault.seal(data, "photo|" + tag))
        tmp.replace(self.photos_dir / name)
        return name

    def photo_bytes(self, u) -> bytes | None:
        """Decrypted photo of user `u`, or None."""
        if not u or not u.get("photo_file"):
            return None
        tag = self.vault.tag(u["id"])
        try:
            return self.vault.open((self.photos_dir / u["photo_file"]).read_bytes(), "photo|" + tag)
        except FileNotFoundError:
            return None
        except Tampered:
            self._tampered(f"photo of {u['id']}")
            return None

    def photo_exists(self, u) -> bool:
        return bool(u and u.get("photo_file") and (self.photos_dir / u["photo_file"]).exists())

    @staticmethod
    def embedding(u):
        return u.get("embedding") or None

    def users_needing_embedding(self):
        """Active users whose embedding is missing or older than their photo (or photo removed)."""
        return [u for u in self.users() if (u.get("embedding_src") or None) != self.photo_version(u)]

    def set_embedding(self, user_id, embedding, src):
        with self._write_lock:
            u = self._all_users().get(str(user_id))
            if u:
                self._save_user({**u, "embedding": embedding, "embedding_src": src})

    def deactivate_missing_users(self, ids):
        keep = {str(i) for i in ids}
        with self._write_lock:
            for uid, u in list(self._all_users().items()):
                if uid not in keep and u.get("active"):
                    self._save_user({**u, "active": False})

    # --------------------------------------------------------- snapshots
    def _save_snapshot(self, uuid, data: bytes) -> str:
        name = f"{uuid}.bin"
        tmp = self.snapshots_dir / (name + ".tmp")
        tmp.write_bytes(self.vault.seal(data, "snapshot|" + uuid))
        tmp.replace(self.snapshots_dir / name)
        return name

    def snapshot_bytes(self, record) -> bytes | None:
        name = record.get("snapshot_file")
        if not name:
            return None
        try:
            data = self.vault.open((self.snapshots_dir / name).read_bytes(), "snapshot|" + record["uuid"])
        except FileNotFoundError:
            return None
        except Tampered:
            self._tampered(f"snapshot of check-in {record['uuid']}")
            return None
        if hashlib.sha256(data).hexdigest() != record.get("snapshot_sha256"):
            self._tampered(f"snapshot of check-in {record['uuid']}")
            return None
        return data

    # ------------------------------------------------------------ checkins
    def _day(self, ts: str) -> str:
        dt = datetime.fromisoformat(ts)
        return (dt.astimezone(self.tz) if self.tz is not None else dt).date().isoformat()

    def _next_seq(self):
        stored = self.get("checkin_seq", 0) or 0
        row = self.conn().execute("SELECT MAX(seq) m FROM checkins2").fetchone()
        return max(int(stored), int(row["m"] or 0)) + 1

    def create_checkin(self, *, uuid, user_id, full_name, direction, ts, status, reason, distance, timezone,
                       relay_name, snapshot: bytes | None, sync_state="pending", synced_at=None):
        """Number, sign, encrypt and store a new check-in (and its snapshot). Returns the record."""
        with self._write_lock:
            seq = self._next_seq()
            snapshot_file = self._save_snapshot(uuid, snapshot) if snapshot else None
            record = {"uuid": uuid, "seq": seq, "relay_id": self.relay_id, "user_id": str(user_id),
                      "direction": direction, "ts": ts, "status": status, "reason": reason, "distance": distance,
                      "timezone": timezone,
                      "snapshot_sha256": hashlib.sha256(snapshot).hexdigest() if snapshot else None}
            record["signature"] = self.identity.sign_checkin(record)
            record.update(full_name=full_name, relay_name=relay_name, snapshot_file=snapshot_file)
            self.set("checkin_seq", seq)
            self.write("INSERT INTO checkins2(uuid, seq, day, user_tag, sealed, sync_state, synced_at) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (uuid, seq, self._day(ts), self.vault.tag(user_id),
                        self.vault.seal_json(record, "checkin|" + uuid), sync_state, synced_at))
            self._checkin_cache[uuid] = record
            return record

    def _rows(self, rows):
        """Decrypted check-ins (with their sync state); changed rows are left out and never sent."""
        out = []
        for row in rows:
            uuid = row["uuid"]
            rec = self._checkin_cache.get(uuid)
            if rec is None:
                try:
                    rec = self.vault.open_json(row["sealed"], "checkin|" + uuid)
                    if (rec.get("uuid") != uuid or rec.get("seq") != row["seq"] or self._day(rec["ts"]) != row["day"]
                            or self.vault.tag(rec["user_id"]) != row["user_tag"]):
                        raise Tampered(uuid)
                except (Tampered, KeyError, ValueError):
                    self._tampered(f"check-in {uuid}")
                    if row["sync_state"] == "pending":
                        self.write("UPDATE checkins2 SET sync_state='tampered' WHERE uuid=?", (uuid,))
                    continue
                if len(self._checkin_cache) > 5000:
                    self._checkin_cache.clear()
                self._checkin_cache[uuid] = rec
            out.append({**rec, "sync_state": row["sync_state"], "sync_error": row["sync_error"],
                        "synced_at": row["synced_at"]})
        return out

    def checkins_for_day(self, day: date):
        return self._rows(self.conn().execute(
            "SELECT * FROM checkins2 WHERE day=? ORDER BY seq DESC", (day.isoformat(),)))

    def last_checkin(self, user_id, day: date):
        rows = self._rows(self.conn().execute(
            "SELECT * FROM checkins2 WHERE user_tag=? AND day=? ORDER BY seq DESC LIMIT 1",
            (self.vault.tag(user_id), day.isoformat())))
        return rows[0] if rows else None

    def pending_checkins(self, limit=20):
        return self._rows(self.conn().execute(
            "SELECT * FROM checkins2 WHERE sync_state='pending' ORDER BY seq LIMIT ?", (limit,)))

    def mark_synced(self, uuids, when):
        for u in uuids:
            self.write("UPDATE checkins2 SET sync_state='synced', sync_error=NULL, synced_at=? WHERE uuid=?", (when, u))

    def mark_rejected(self, uuid, error):
        self.write("UPDATE checkins2 SET sync_state='rejected', sync_error=? WHERE uuid=?", (error, uuid))

    def retry_rejected(self):
        self.write("UPDATE checkins2 SET sync_state='pending', sync_error=NULL WHERE sync_state='rejected'")

    def counts(self):
        rows = self.conn().execute("SELECT sync_state, COUNT(*) n FROM checkins2 GROUP BY sync_state").fetchall()
        return {r["sync_state"]: r["n"] for r in rows}

    # ------------------------------------------- upgrade from the plain cache
    def _migrate_v1(self):
        """Encrypt the cache of an older relay version (plain tables users / checkins / kv and plain
        photo and snapshot files), then remove the plain copies."""
        c = self.conn()
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "checkins", "kv"} & tables:
            return
        log.warning("Encrypting the relay's local data (upgrade from an older version)…")
        with self._write_lock:
            if "kv" in tables:
                for r in c.execute("SELECT key, value FROM kv").fetchall():
                    try:
                        self.set(r["key"], json.loads(r["value"]))
                    except ValueError:
                        pass
            if "users" in tables:
                for r in c.execute("SELECT * FROM users").fetchall():
                    r = dict(r)
                    photo_file, ext = None, None
                    old = self.photos_dir / r["photo_file"] if r.get("photo_file") else None
                    if old and old.is_file():
                        ext = old.suffix.lower() or ".jpg"
                        photo_file = self.save_photo(r["id"], old.read_bytes(), ext)
                        old.unlink()
                    self._save_user({
                        "id": r["id"], "full_name": r["full_name"],
                        "descriptor": json.loads(r["descriptor"]) if r.get("descriptor") else None,
                        "has_photo": bool(r["has_photo"]), "photo_file": photo_file, "photo_ext": ext,
                        "active": bool(r["active"]), "updated_at": r["updated_at"],
                        # recomputed from the photo (the plain value can't be trusted)
                        "embedding": None, "embedding_src": None})
            if "checkins" in tables:
                rows = [dict(r) for r in c.execute("SELECT * FROM checkins").fetchall()]
                # already synced first, then the pending ones in time order: the pending ones get
                # consecutive numbers, so the main server sees no gap
                rows.sort(key=lambda r: (r["sync_state"] != "synced", r["ts"]))
                for r in rows:
                    snap = None
                    if r.get("snapshot_file"):
                        p = self.snapshots_dir / r["snapshot_file"]
                        if p.is_file():
                            snap = p.read_bytes()
                            p.unlink()
                    state = "synced" if r["sync_state"] == "synced" else "pending"
                    self.create_checkin(uuid=r["uuid"], user_id=r["user_id"], full_name=r["full_name"],
                                        direction=r["direction"], ts=r["ts"], status=r["status"],
                                        reason=r["reason"], distance=r["distance"], timezone=r.get("timezone"),
                                        relay_name=r.get("relay_name"), snapshot=snap, sync_state=state,
                                        synced_at=r.get("synced_at"))
            for t in ("users", "checkins", "kv"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
            c.commit()
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # plain files left over (photos of removed users, old snapshots)
        for d in (self.photos_dir, self.snapshots_dir):
            for p in d.glob("*"):
                if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    p.unlink()
        self._users_cache = None
        log.warning("Local data encrypted")

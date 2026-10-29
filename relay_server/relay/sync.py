"""Background synchronisation with the Main server.

Every `sync_interval` seconds (or immediately when woken up after a new check-in):
  1. ping            -> Main server status: REACHABLE / UNREACHABLE / ERROR
  2. settings        -> cached locally (max in time, min out time)
  3. users           -> cached locally with their photos (face data is computed from them here)
  4. pending checkins-> pushed in batches; kept locally until accepted
"""
import base64
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .config import Config
from .db import Database

log = logging.getLogger("relay.sync")

REACHABLE, UNREACHABLE, ERROR, UNKNOWN = "Reachable", "Unreachable", "Error", "Unknown"


class AuthError(Exception):
    pass


@dataclass
class SyncState:
    status: str = UNKNOWN
    message: str = "Not synchronised yet"
    site_name: str = ""
    last_ok: datetime | None = None
    last_attempt: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return {k: getattr(self, k) for k in ("status", "message", "site_name", "last_ok", "last_attempt")}


class SyncWorker(threading.Thread):
    def __init__(self, cfg: Config, db: Database, clock=None, on_users_synced=None):
        super().__init__(daemon=True, name="sync")
        self.cfg, self.db, self.clock = cfg, db, clock
        self.on_users_synced = on_users_synced  # recompute face data of new/changed photos
        self.state = SyncState(site_name=db.get("site_name", ""))
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {cfg.token}"
        self.http.verify = cfg.verify_ssl

    # ---------------------------------------------------------------- control
    def wake(self):
        self._wake.set()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    def run(self):
        while not self._stopping.is_set():
            self.sync_once()
            self._wake.wait(self.cfg.sync_interval)
            self._wake.clear()

    def _now(self):
        """Trusted time when available (display/bookkeeping only)."""
        now = self.clock.now() if self.clock else None
        return now or datetime.now().astimezone()

    # ------------------------------------------------------------------- http
    def _call(self, method, path, **kw):
        r = self.http.request(method, self.cfg.main_url + "/api/v1" + path, timeout=kw.pop("timeout", 15), **kw)
        if r.status_code in (401, 403):
            try:
                err = r.json().get("error", r.reason)
            except ValueError:
                err = r.reason
            raise AuthError({"invalid_token": "auth token invalid", "missing_token": "auth token missing",
                             "ip_not_allowed": "IP address not allowed", "relay_disabled": "relay disabled"}
                            .get(err, err))
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------- sync
    def sync_once(self):
        now = self._now()
        self.state.update(last_attempt=now)
        if not self.cfg.token:
            self.state.update(status=ERROR, message="No token configured in relay.ini")
            return
        try:
            info = self._call("GET", "/ping", timeout=8).json()
            self.state.update(site_name=info.get("relay", ""))
            self.db.set("site_name", info.get("relay", ""))
            self.db.set("settings", self._call("GET", "/settings").json())
            self._sync_users()
            if self.on_users_synced:
                self.on_users_synced()
            pushed = self._push_checkins()
            self.state.update(status=REACHABLE, last_ok=self._now(),
                              message=f"Synchronised ({pushed} check-in(s) sent)" if pushed else "Synchronised")
        except AuthError as exc:
            log.warning("Main server refused the relay: %s", exc)
            self.state.update(status=ERROR, message=str(exc))
        except requests.RequestException as exc:
            log.info("Main server unreachable: %s", exc)
            self.state.update(status=UNREACHABLE, message=type(exc).__name__)
        except Exception as exc:  # never kill the thread
            log.exception("Sync failed")
            self.state.update(status=ERROR, message=f"Unexpected error: {exc}")

    def _sync_users(self):
        users = self._call("GET", "/users").json()["users"]
        for u in users:
            local = self.db.user(u["id"])
            photo_file = local["photo_file"] if local else None
            photo_missing = not photo_file or not (self.cfg.photos_dir / photo_file).exists()
            if u.get("has_photo") and (photo_missing or not local or local["updated_at"] != u["updated_at"]):
                photo_file = self._download_photo(u["id"]) or photo_file
            elif not u.get("has_photo"):
                photo_file = None
            self.db.upsert_user(u, photo_file)
        self.db.deactivate_missing_users([u["id"] for u in users])
        self.db.set("users_version", max((u["updated_at"] or "" for u in users), default="") + f"#{len(users)}")

    def _download_photo(self, user_id):
        try:
            r = self._call("GET", f"/users/{user_id}/photo", timeout=30)
        except requests.HTTPError:
            return None
        ext = ".png" if "png" in r.headers.get("Content-Type", "") else ".jpg"
        name = f"{user_id}{ext}"
        (self.cfg.photos_dir / name).write_bytes(r.content)
        return name

    def _push_checkins(self):
        total = 0
        while True:
            batch = self.db.pending_checkins(limit=20)
            if not batch:
                return total
            payload = []
            for c in batch:
                item = {k: c[k] for k in ("uuid", "user_id", "direction", "status", "reason", "distance",
                                          "timezone")}
                item["timestamp"] = c["ts"]
                snap = self.cfg.snapshots_dir / c["snapshot_file"] if c["snapshot_file"] else None
                if snap and snap.exists():
                    item["snapshot"] = base64.b64encode(snap.read_bytes()).decode()
                payload.append(item)
            res = self._call("POST", "/checkins", json={"checkins": payload}, timeout=60).json()
            when = self._now().isoformat(timespec="seconds")
            self.db.mark_synced(res.get("accepted", []), when)
            for rej in res.get("rejected", []):
                self.db.mark_rejected(rej["uuid"], rej.get("error", "rejected"))
            total += len(res.get("accepted", []))
            if not res.get("accepted") and not res.get("rejected"):
                return total  # avoid looping on an unexpected answer

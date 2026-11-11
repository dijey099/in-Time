"""Background synchronisation with the Main server.

Every `sync_interval` seconds (or immediately when woken up after a new check-in):
  1. ping            -> Main server status: REACHABLE / UNREACHABLE / ERROR
  2. settings        -> cached locally (max in time, min out time)
  3. collaborators   -> cached locally with their photos (face data is computed from them here)
  4. pending checkins-> pushed in batches; kept locally until accepted

Every request is signed with the relay's identity key (X-Relay-Time / X-Relay-Signature), and each
check-in carries the signature made when it was created: the main server accepts nothing else.
"""
import base64
import json
import logging
import re
import threading
import time
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
    def __init__(self, cfg: Config, db: Database, clock=None, on_collaborators_synced=None, machine_id=None,
                 on_settings=None, identity=None):
        super().__init__(daemon=True, name="sync")
        self.cfg, self.db, self.clock, self.identity = cfg, db, clock, identity
        self.on_collaborators_synced = on_collaborators_synced  # recompute face data of new/changed photos
        self.on_settings = on_settings          # settings from the main server (e.g. min similarity)
        self.state = SyncState(site_name=db.get("site_name", ""))
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {cfg.token}"
        if machine_id:  # lets the main server enforce one relay per token
            self.http.headers["X-Relay-Id"] = machine_id
        if identity:  # recorded by the main server at the first request, then used to check signatures
            self.http.headers["X-Relay-Key"] = identity.public_key
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
    def _signed_headers(self, method, path, body: bytes):
        """Proof that the request comes from this relay (its private key never leaves it)."""
        if not self.identity:
            return {}
        now = self.clock.now_utc() if self.clock else None
        ts = int(now.timestamp()) if now else int(time.time())  # trusted internet time when available
        return {"X-Relay-Time": str(ts),
                "X-Relay-Signature": self.identity.sign_request(method, "/api/v1" + path, ts, body)}

    def _call(self, method, path, **kw):
        body = b""
        if "json" in kw:
            body = json.dumps(kw.pop("json"), separators=(",", ":")).encode()
            kw["data"] = body
            kw.setdefault("headers", {})["Content-Type"] = "application/json"
        kw.setdefault("headers", {}).update(self._signed_headers(method, path, body))
        r = self.http.request(method, self.cfg.main_url + "/api/v1" + path, timeout=kw.pop("timeout", 15), **kw)
        if r.status_code in (401, 403):
            try:
                err = r.json().get("error", r.reason)
            except ValueError:
                err = r.reason
            raise AuthError({"invalid_token": "auth token invalid", "missing_token": "auth token missing",
                             "ip_not_allowed": "IP address not allowed", "relay_disabled": "relay disabled",
                             "token_in_use": "this token is already used by another relay server",
                             "missing_relay_id": "relay ID missing (update the relay)",
                             "missing_signature": "request not signed (update the relay)",
                             "missing_relay_key": "relay key missing (update the relay)",
                             "bad_signature": "the request signature does not match the key recorded for "
                                              "this token (another relay used it first: generate a new "
                                              "token on the main server) or the request was altered",
                             "clock_skew": "the main server's clock is wrong (more than 5 min from the "
                                           "internet time)",
                             "relay_id_in_use": "this relay server already uses another token on the main "
                                                "server"}
                            .get(err, err))
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------- sync
    def sync_once(self):
        now = self._now()
        self.state.update(last_attempt=now)
        if not self.cfg.token:
            self.state.update(status=ERROR, message="No token configured in relay.conf")
            return
        try:
            info = self._call("GET", "/ping", timeout=8).json()
            self.state.update(site_name=info.get("relay", ""))
            self.db.set("site_name", info.get("relay", ""))
            settings = self._call("GET", "/settings").json()
            self.db.set("settings", settings)
            if self.on_settings:
                self.on_settings(settings)
            self._sync_collaborators()
            if self.on_collaborators_synced:
                self.on_collaborators_synced()
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

    def _sync_collaborators(self):
        collaborators = self._call("GET", "/collaborators").json()["collaborators"]
        for u in collaborators:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(u.get("id", ""))):
                log.warning("Ignored collaborator %r: invalid ID", u.get("id"))
                continue
            local = self.db.collaborator(u["id"])
            photo_file = local["photo_file"] if local else None
            photo_ext = None
            photo_missing = not self.db.photo_exists(local)
            if u.get("has_photo") and (photo_missing or not local or local["updated_at"] != u["updated_at"]):
                got = self._download_photo(u["id"])
                if got:
                    photo_file, photo_ext = got
            elif not u.get("has_photo"):
                photo_file = None
            self.db.upsert_collaborator(u, photo_file, photo_ext)
        self.db.deactivate_missing_collaborators([u["id"] for u in collaborators])
        self.db.set("collaborators_version", max((u["updated_at"] or "" for u in collaborators), default="") + f"#{len(collaborators)}")

    def _download_photo(self, collaborator_id):
        try:
            r = self._call("GET", f"/collaborators/{collaborator_id}/photo", timeout=30)
        except requests.HTTPError:
            return None
        ext = ".png" if "png" in r.headers.get("Content-Type", "") else ".jpg"
        return self.db.save_photo(collaborator_id, r.content, ext), ext

    def _push_checkins(self):
        total = 0
        while True:
            batch = self.db.pending_checkins(limit=20)
            if not batch:
                return total
            payload = []
            for c in batch:
                # exactly the values the relay signed when the check-in was made
                item = {k: c[k] for k in ("uuid", "seq", "collaborator_id", "direction", "status", "reason", "distance",
                                          "timezone", "snapshot_sha256", "signature")}
                item["timestamp"] = c["ts"]
                if c.get("location") is not None:  # GPS location of the phone (signed)
                    item["location"] = c["location"]
                snap = self.db.snapshot_bytes(c)
                if snap:
                    item["snapshot"] = base64.b64encode(snap).decode()
                payload.append(item)
            res = self._call("POST", "/checkins", json={"checkins": payload}, timeout=60).json()
            when = self._now().isoformat(timespec="seconds")
            self.db.mark_synced(res.get("accepted", []), when)
            for rej in res.get("rejected", []):
                self.db.mark_rejected(rej["uuid"], rej.get("error", "rejected"))
            total += len(res.get("accepted", []))
            if not res.get("accepted") and not res.get("rejected"):
                return total  # avoid looping on an unexpected answer

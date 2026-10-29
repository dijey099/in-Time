"""Private API used by phones on the same network, plus the check-in web page
(HTML/CSS/JS) served from relay/web/.

The phone only takes the picture. The relay identifies the person (POST /api/identify),
then accepts the check-in only with the one-time token it issued (POST /api/checkins).
"""
import base64
import binascii
import ipaddress
import logging
import threading
import socket
import uuid as uuidlib
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.serving import make_server

from .config import Config
from .db import Database
from .face_engine import NoFace
from .rules import evaluate, suggest_direction

log = logging.getLogger("relay.web")


NO_INTERNET = "No internet connection: check-in is unavailable. Please try again later."


def create_web_app(cfg: Config, db: Database, clock, identifier, on_new_checkin=lambda: None, sync_state=None,
                   model_status=None) -> Flask:
    """`clock` is the TrustedClock: all times come from it, never from the PC."""
    app = Flask(__name__, static_folder=str(cfg.web_dir), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

    @app.before_request
    def lan_only():
        if cfg.allow_public_clients:
            return
        try:
            ip = ipaddress.ip_address(request.remote_addr or "")
        except ValueError:
            abort(403)
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            abort(403)

    @app.after_request
    def no_cache_api(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/vendor/face-api/<path:filename>")
    def face_api(filename):
        # served from cfg.vendor_dir: outside the bundle in an executable (see config.py)
        return send_from_directory(cfg.vendor_dir, filename, max_age=86400)

    @app.get("/")
    def index():
        return send_from_directory(cfg.web_dir, "index.html")

    @app.get("/api/status")
    def status():
        main = sync_state.snapshot()["status"] if sync_state else None
        now = clock.now_utc()
        return jsonify(site=db.get("site_name", ""), settings=db.get("settings", {}), main_server=main,
                       internet_ok=now is not None, internet_message=clock.status()["message"],
                       server_time=int(now.timestamp() * 1000) if now else None,
                       utc_offset_min=int(clock.now().utcoffset().total_seconds() // 60) if now else None,
                       models_ok=(model_status.ok if model_status else True) and identifier.ok,
                       models_message=(model_status.message if model_status and not model_status.ok
                                       else identifier.message))

    @app.post("/api/identify")
    def identify():
        """Body: {"snapshot": "data:image/jpeg;base64,..."} taken by the phone.
        The relay finds and recognises the face itself."""
        now = clock.now()
        if now is None:
            return jsonify(error=NO_INTERNET), 503
        if not identifier.ok:
            return jsonify(error=identifier.message), 503
        if not identifier.allow(request.remote_addr):
            return jsonify(error="Too many attempts, wait a few seconds"), 429
        jpeg = _jpeg(request.get_json(silent=True) or {})
        if jpeg is None:
            return jsonify(error="Missing or invalid snapshot"), 400
        try:
            user, sim = identifier.identify(jpeg)
        except NoFace as exc:
            return jsonify(match=False, reason="no_face", message=str(exc))
        if user is None:
            log.info("Face not recognised (best similarity %.2f)", sim)
            return jsonify(match=False, reason="unknown", message="Face not recognised")
        token = identifier.issue(user, sim, jpeg, request.remote_addr)
        last = db.last_checkin(user["id"], now.date())
        return jsonify(match=True, token=token, user={"id": user["id"], "full_name": user["full_name"]},
                       direction=suggest_direction(last), photo=f"api/photo/{token}")

    @app.get("/api/photo/<token>")
    def recognised_photo(token):
        """Reference photo of the person just recognised (only with their token)."""
        t = identifier.peek(token, request.remote_addr)
        u = db.user(t["user_id"]) if t else None
        if not u or not u["photo_file"]:
            abort(404)
        return send_from_directory(cfg.photos_dir, u["photo_file"])

    @app.post("/api/checkins")
    def create_checkin():
        """Body: {"token": <from /api/identify>, "direction": "IN" | "OUT"}"""
        now = clock.now()  # trusted time (Google), None without internet
        if now is None:
            log.warning("Check-in refused: no internet / trusted time")
            return jsonify(error=NO_INTERNET), 503
        data = request.get_json(silent=True) or {}
        t = identifier.consume(str(data.get("token", "")), request.remote_addr)
        if t is None:
            return jsonify(error="Recognition expired: look at the camera again"), 403
        user = db.user(t["user_id"])
        if not user or not user["active"]:
            return jsonify(error="Unknown user"), 404

        last = db.last_checkin(user["id"], now.date())
        direction = data.get("direction") or suggest_direction(last)
        if direction not in ("IN", "OUT"):
            return jsonify(error="Bad direction"), 400
        if last and last["direction"] == direction:
            elapsed = (now - datetime.fromisoformat(last["ts"])).total_seconds()
            if elapsed < cfg.duplicate_window:
                return jsonify(error=f"Already checked {direction} at {last['ts'][11:16]}"), 409

        status, reason = evaluate(direction, now, db.get("settings", {}))
        cid = str(uuidlib.uuid4())
        # the snapshot is the picture the relay recognised, never one sent afterwards
        (cfg.snapshots_dir / f"{cid}.jpg").write_bytes(t["snapshot"])

        db.add_checkin({
            "uuid": cid, "user_id": user["id"], "full_name": user["full_name"], "direction": direction,
            "ts": now.isoformat(timespec="seconds"), "status": status, "reason": reason,
            # face distance = 1 - similarity (lower = closer)
            "distance": round(1 - t["similarity"], 4), "snapshot_file": f"{cid}.jpg",
            "timezone": cfg.tz_name, "relay_name": db.get("site_name", "") or socket.gethostname(),
        })
        log.info("Check-in %s %s %s %s (similarity %.2f)", user["id"], direction, status, reason or "",
                 t["similarity"])
        on_new_checkin()
        return jsonify(ok=True, checkin={"user_id": user["id"], "full_name": user["full_name"],
                                         "direction": direction, "status": status, "reason": reason,
                                         "time": now.strftime("%H:%M")}), 201

    return app


def _jpeg(data):
    snap = str(data.get("snapshot") or "")
    if not snap.startswith("data:image/jpeg;base64,"):
        return None
    try:
        raw = base64.b64decode(snap.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if raw[:3] == b"\xff\xd8\xff" else None


class WebServer(threading.Thread):
    def __init__(self, app, host, port, cert, key):
        super().__init__(daemon=True, name="web")
        self.server = make_server(host, port, app, threaded=True, ssl_context=(str(cert), str(key)))

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()

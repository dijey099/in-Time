"""Public API used by relay servers."""
import base64
import binascii
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request, send_from_directory
from sqlalchemy.exc import IntegrityError

from .auth import relay_required
from .models import Checkin, Setting, User, db, utcnow

api_bp = Blueprint("api", __name__)


def effective_settings(relay):
    return {
        "max_in_time": relay.max_in_time or Setting.get("max_in_time"),
        "min_out_time": relay.min_out_time or Setting.get("min_out_time"),
    }


@api_bp.get("/ping")
@relay_required
def ping():
    return jsonify(ok=True, relay=g.relay.name, server_time=utcnow().isoformat())


@api_bp.get("/settings")
@relay_required
def settings():
    return jsonify(effective_settings(g.relay))


@api_bp.get("/users")
@relay_required
def users():
    """Full user list (including inactive ones so relays can drop them)."""
    rows = User.query.order_by(User.id).all()
    return jsonify(users=[u.to_api() for u in rows])


@api_bp.get("/users/<user_id>/photo")
@relay_required
def user_photo(user_id):
    user = db.session.get(User, user_id)
    if not user or not user.photo_path:
        return jsonify(error="not_found"), 404
    return send_from_directory(current_app.config["PHOTOS_DIR"], user.photo_path.split("/")[-1])


def _face_check(uuid):
    """Compare the snapshot with the user's photo now if possible (else the background worker does)."""
    from .verify import engine, verify_checkin
    if not engine().ok:
        return
    try:
        c = Checkin.query.filter_by(uuid=uuid).first()
        verify_checkin(c)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Face check of %s failed (retried later)", uuid)


def _parse_ts(value):
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return ts


@api_bp.post("/checkins")
@relay_required
def push_checkins():
    """Body: {"checkins": [{uuid, user_id, direction, timestamp, status, reason,
    distance, timezone, snapshot (base64 jpeg)}]}

    Each check-in is stored, then its snapshot is compared with the user's photo
    (server-side face check); a check-in without snapshot is flagged.

    The relay name stored with each check-in is the authenticated relay's name
    (from its token), never a value sent in the body.

    Idempotent: already known uuids are reported as accepted.
    Response: {"accepted": [uuid...], "rejected": [{uuid, error}...]}
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("checkins") or []
    accepted, rejected = [], []

    for item in items:
        uuid = str(item.get("uuid", ""))[:36]
        try:
            if not uuid:
                raise ValueError("missing uuid")
            if Checkin.query.filter_by(uuid=uuid).first():
                accepted.append(uuid)
                continue
            user_id = str(item["user_id"])
            if not db.session.get(User, user_id):
                raise ValueError("unknown user")
            direction = item["direction"]
            if direction not in ("IN", "OUT"):
                raise ValueError("bad direction")
            status = item.get("status", "OK")
            if status not in ("OK", "KO"):
                raise ValueError("bad status")

            snapshot_path = None
            if item.get("snapshot"):
                data = base64.b64decode(item["snapshot"], validate=True)
                name = f"{uuid}.jpg"
                (current_app.config["SNAPSHOTS_DIR"] / name).write_bytes(data)
                snapshot_path = f"snapshots/{name}"

            db.session.add(
                Checkin(
                    uuid=uuid,
                    user_id=user_id,
                    relay_id=g.relay.id,
                    direction=direction,
                    timestamp=_parse_ts(item["timestamp"]),
                    status=status,
                    reason=(item.get("reason") or None),
                    distance=item.get("distance"),
                    snapshot_path=snapshot_path,
                    timezone=(str(item.get("timezone") or "")[:64] or None),
                    relay_name=g.relay.name,
                )
            )
            db.session.commit()
            accepted.append(uuid)
            _face_check(uuid)
        except (KeyError, ValueError, binascii.Error, IntegrityError) as exc:
            db.session.rollback()
            rejected.append({"uuid": uuid, "error": str(exc)})

    return jsonify(accepted=accepted, rejected=rejected)

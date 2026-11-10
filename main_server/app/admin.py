"""Admin web UI: users, relays, settings, check-ins."""
import csv
import io
import json
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, redirect,
                   render_template, request, send_from_directory, session, url_for)

from urllib.parse import urlsplit

from .auth import (check_admin_credentials, clear_login_failures, client_ip, csrf_protect, csrf_token,
                   login_blocked, login_required, record_login_failure)
from .face_engine import NoFace
from .models import Checkin, Relay, Setting, User, db, hash_token, tz_from_name
from . import verify

admin_bp = Blueprint("admin", __name__)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PHOTO_EXT = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png"}


@admin_bp.before_request
def _csrf():
    csrf_protect()


@admin_bp.app_template_filter("km")
def _km(metres):
    """Distance for display: "85 m", "1.2 km"."""
    if metres is None:
        return ""
    return f"{metres:.0f} m" if metres < 1000 else f"{metres / 1000:.1f} km"


@admin_bp.app_context_processor
def _inject():
    models = current_app.extensions.get("face_models")
    return {"csrf_token": csrf_token, "face_models": models, "face_engine": current_app.extensions.get("face_engine"),
            "source_url": current_app.config.get("SOURCE_URL"),
            "map_config": {"tiles": current_app.config.get("MAP_TILE_URL"),
                           "attribution": current_app.config.get("MAP_TILE_ATTRIBUTION"),
                           "search": current_app.config.get("MAP_SEARCH_URL")}}


# ---------------------------------------------------------------------- login
def _safe_next(nxt: str):
    """Only a path on this site: no scheme, no host, no backslash (browsers read '/\\x' as '//x')."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(ch) < 32 for ch in nxt):
        return None
    parts = urlsplit(nxt)
    return nxt if not parts.scheme and not parts.netloc else None


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip() or request.remote_addr or "?"
        if login_blocked(ip):
            current_app.logger.warning("Admin login refused (locked) from %s", ip)
            flash("The admin login is locked after too many failed attempts. An administrator must unlock it "
                  "on the server (delete the admin_login.lock file).", "error")
            return render_template("login.html"), 423
        if check_admin_credentials(request.form.get("username", ""), request.form.get("password", "")):
            clear_login_failures(ip)
            session.clear()
            session["admin"] = True
            return redirect(_safe_next(request.args.get("next", "")) or url_for("admin.dashboard"))
        record_login_failure(ip)
        current_app.logger.warning("Failed admin login from %s", ip)
        flash("Invalid credentials", "error")
    return render_template("login.html")


@admin_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# ------------------------------------------------------------------ dashboard
def _day_bounds(d: date):
    # Local day of the server
    start = datetime.combine(d, time.min).astimezone()
    return start, start + timedelta(days=1)


@admin_bp.get("/")
@login_required
def dashboard():
    start, end = _day_bounds(date.today())
    today = (Checkin.query.filter(Checkin.timestamp >= start, Checkin.timestamp < end)
             .order_by(Checkin.timestamp.desc()).all())
    stats = {
        "users": User.query.filter_by(active=True).count(),
        "missing_face": User.query.filter(User.active.is_(True), User.face_embedding.is_(None)).count(),
        "today": len(today),
        "late": sum(1 for c in today if c.status == "KO"),
        "flagged": sum(1 for c in today if c.flagged),
        "outside": sum(1 for c in today if c.outside_area),
    }
    outside = [c for c in today if c.outside_area][:10]
    relays = Relay.query.order_by(Relay.name).all()
    return render_template("dashboard.html", checkins=today[:50], stats=stats, relays=relays,
                           outside=outside, now=datetime.now(timezone.utc),
                           relays_without_area=[r for r in relays if r.enabled and not r.has_area])


# ---------------------------------------------------------------------- users
@admin_bp.get("/users")
@login_required
def users():
    q = request.args.get("q", "").strip()
    dept = request.args.get("department", "")
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter((User.id.ilike(like)) | (User.full_name.ilike(like)))
    if dept == "__none__":
        query = query.filter((User.department.is_(None)) | (User.department == ""))
    elif dept:
        query = query.filter(User.department == dept)
    departments = current_app.config["DEPARTMENTS"]
    # users with no department, or one that is no longer in DEPARTMENTS
    invalid = User.query.filter((User.department.is_(None)) | (User.department == "")
                                | (User.department.notin_(departments) if departments else True)).count()
    return render_template("users.html", users=query.order_by(User.id).all(), q=q, dept=dept,
                           departments=departments, invalid_count=invalid)


def _photo_ext(file_storage):
    ext = PHOTO_EXT.get(("." + file_storage.filename.rsplit(".", 1)[-1]).lower()) if "." in file_storage.filename else None
    if not ext:
        raise ValueError("Photo must be a .jpg, .jpeg or .png file")
    return ext


def _save_photo(user: User, data: bytes, ext: str):
    photos_dir = current_app.config["PHOTOS_DIR"]
    for old in (".jpg", ".png"):  # drop previous photo with another extension
        (photos_dir / f"{user.id}{old}").unlink(missing_ok=True)
    name = f"{user.id}{ext}"
    (photos_dir / name).write_bytes(data)
    user.photo_path = f"photos/{name}"


def _parse_descriptor(raw):
    if not raw:
        return None
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != 128:
        raise ValueError("Invalid face descriptor")
    return [float(v) for v in values]


@admin_bp.route("/users/new", methods=["GET", "POST"])
@admin_bp.route("/users/<user_id>/edit", methods=["GET", "POST"])
@login_required
def user_form(user_id=None):
    user = db.session.get(User, user_id) if user_id else None
    if user_id and not user:
        abort(404)

    if request.method == "POST":
        try:
            if user is None:
                new_id = request.form.get("id", "").strip()
                if not ID_RE.match(new_id):
                    raise ValueError("ID may only contain letters, digits, '-' and '_'")
                if db.session.get(User, new_id):
                    raise ValueError(f"User {new_id} already exists")
                user = User(id=new_id)
                db.session.add(user)
            full_name = request.form.get("full_name", "").strip()
            if not full_name:
                raise ValueError("Full name is required")
            user.full_name = full_name
            departments = current_app.config["DEPARTMENTS"]
            if not departments:
                raise ValueError("No departments configured: set DEPARTMENTS in the main server .env")
            department = request.form.get("department", "").strip()
            if department not in departments:
                raise ValueError("Please choose the user's department")
            user.department = department
            user.active = request.form.get("active") == "on"

            photo = request.files.get("photo")
            if photo and photo.filename:
                ext = _photo_ext(photo)
                data = photo.read()
                descriptor = _parse_descriptor(request.form.get("descriptor"))
                allow_no_face = request.form.get("allow_no_face") == "on"
                embedding = None
                if verify.engine().ok:  # the server decides: it checks every check-in against this photo
                    try:
                        embedding = verify.engine().to_list(verify.engine().embed(data))
                    except NoFace as exc:
                        if not allow_no_face:
                            raise ValueError(f"No usable face in the photo ({exc}). Use a clear, front-facing "
                                             f"photo with a single face.")
                elif descriptor is None and not allow_no_face:  # models still loading: browser check
                    raise ValueError("No face detected in the photo")
                _save_photo(user, data, ext)
                user.descriptor = descriptor
                user.face_embedding = embedding
                user.face_model = verify.MODEL_ID if verify.engine().ok else None  # else: done by the worker
            db.session.commit()
            flash(f"User {user.id} saved", "ok")
            return redirect(url_for("admin.users"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            if user_id is None:
                user = None

    return render_template("user_form.html", user=user, departments=current_app.config["DEPARTMENTS"])


@admin_bp.post("/users/check-faces")
@login_required
def users_check_faces():
    """Compute the server face data of photos not processed yet."""
    if not verify.engine().ok:
        flash(verify.engine().message, "error")
        return redirect(url_for("admin.users"))
    done, failed = 0, []
    for u in verify.users_to_process():
        try:
            verify.compute_user_face(u)
            done += 1
        except (NoFace, OSError) as exc:
            u.face_model = verify.MODEL_ID
            failed.append(f"{u.id} ({exc})")
        db.session.commit()
    flash(f"{done} photo(s) processed" + (f"; no usable face: {', '.join(failed)}" if failed else ""),
          "error" if failed else "ok")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<user_id>/delete")
@login_required
def user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if Checkin.query.filter_by(user_id=user.id).first():
        user.active = False
        flash(f"User {user.id} has check-ins: deactivated instead of deleted", "ok")
    else:
        if user.photo_path:
            (current_app.config["PHOTOS_DIR"] / user.photo_path.split("/")[-1]).unlink(missing_ok=True)
        db.session.delete(user)
        flash(f"User {user.id} deleted", "ok")
    db.session.commit()
    return redirect(url_for("admin.users"))


@admin_bp.get("/photos/<path:name>")
@login_required
def photo(name):
    return send_from_directory(current_app.config["PHOTOS_DIR"], name)


@admin_bp.get("/snapshots/<path:name>")
@login_required
def snapshot(name):
    return send_from_directory(current_app.config["SNAPSHOTS_DIR"], name)


# --------------------------------------------------------------------- relays
AREA_MIN_RADIUS, AREA_MAX_RADIUS = 10, 50000  # metres


def _validate_area(relay: Relay, required: bool):
    """Site area from the map picker (hidden fields area_lat / area_lng / area_radius)."""
    if "area_lat" not in request.form and not required:
        return  # form without the map: keep the current area
    raw = [request.form.get(k, "").strip() for k in ("area_lat", "area_lng", "area_radius")]
    if not any(raw):
        if required:
            raise ValueError("Pick the site area on the map: click the site's location, then set the radius")
        relay.area_lat = relay.area_lng = relay.area_radius = None
        return
    try:
        lat, lng, radius = (float(v) for v in raw)
    except ValueError:
        raise ValueError("Invalid site area: pick it again on the map") from None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Invalid site area: pick it again on the map")
    if not (AREA_MIN_RADIUS <= radius <= AREA_MAX_RADIUS):
        raise ValueError(f"Area radius must be between {AREA_MIN_RADIUS} m and {AREA_MAX_RADIUS // 1000} km")
    relay.area_lat, relay.area_lng, relay.area_radius = round(lat, 6), round(lng, 6), round(radius)


def _validate_relay_form(relay: Relay, new=False):
    _validate_area(relay, required=new)
    relay.name = request.form.get("name", "").strip() or relay.name
    if not relay.name:
        raise ValueError("Name is required")
    relay.allowed_ips = request.form.get("allowed_ips", "").strip()
    if not relay.allowed_ips:
        raise ValueError("At least one allowed IP (or '*') is required")
    for key in ("max_in_time", "min_out_time"):
        value = request.form.get(key, "").strip()
        if value and not TIME_RE.match(value):
            raise ValueError(f"{key} must be HH:MM")
        setattr(relay, key, value or None)
    tz = request.form.get("timezone", "").strip()
    if tz and not tz_from_name(tz):
        raise ValueError("Time zone must be an IANA name (e.g. Indian/Antananarivo) or UTC+03:00")
    if tz or "timezone" in request.form:
        relay.timezone = tz or None
    relay.enabled = request.form.get("enabled") == "on"
    relay.local_relay = request.form.get("local_relay") == "on"


@admin_bp.route("/relays", methods=["GET", "POST"])
@login_required
def relays():
    if request.method == "POST":
        token = Relay.new_token()
        relay = Relay(token_hash=hash_token(token))
        try:
            _validate_relay_form(relay, new=True)
            if Relay.query.filter_by(name=relay.name).first():
                raise ValueError("A relay with this name already exists")
            db.session.add(relay)
            db.session.commit()
            session["new_token"] = {"name": relay.name, "token": token}
            return redirect(url_for("admin.relays"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    new_token = session.pop("new_token", None)
    relays = Relay.query.order_by(Relay.name).all()
    with_area = next((r for r in relays if r.has_area), None)
    return render_template("relays.html", relays=relays, new_token=new_token, now=datetime.now(timezone.utc),
                           form=request.form, area_limits=(AREA_MIN_RADIUS, AREA_MAX_RADIUS),
                           area_fallback={"lat": with_area.area_lat, "lng": with_area.area_lng} if with_area else None)


@admin_bp.post("/relays/<int:relay_id>/edit")
@login_required
def relay_edit(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    try:
        _validate_relay_form(relay)
        db.session.commit()
        flash(f"Relay {relay.name} updated", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.relays"))


@admin_bp.post("/relays/<int:relay_id>/token")
@login_required
def relay_token(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    token = Relay.new_token()
    relay.token_hash = hash_token(token)
    relay.instance_id = None  # free the one-relay binding: a new/replacement relay can use the token
    relay.public_key = None
    db.session.commit()
    session["new_token"] = {"name": relay.name, "token": token}
    return redirect(url_for("admin.relays"))


@admin_bp.post("/relays/<int:relay_id>/delete")
@login_required
def relay_delete(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    if Checkin.query.filter_by(relay_id=relay.id).first():
        relay.enabled = False
        flash(f"Relay {relay.name} has check-ins: disabled instead of deleted", "ok")
    else:
        db.session.delete(relay)
        flash(f"Relay {relay.name} deleted", "ok")
    db.session.commit()
    return redirect(url_for("admin.relays"))


# ------------------------------------------------------------------- settings
@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        try:
            for key in Setting.DEFAULTS:
                value = request.form.get(key, "").strip()
                if not TIME_RE.match(value):
                    raise ValueError(f"{key} must be HH:MM")
                Setting.set(key, value)
            db.session.commit()
            flash("Settings saved. Relays pick them up on their next sync.", "ok")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    values = {k: Setting.get(k) for k in Setting.DEFAULTS}
    return render_template("settings.html", values=values)


# ------------------------------------------------------------------- checkins
def _parse_day(value: str) -> date:
    """dd/mm/yyyy (forms) or yyyy-mm-dd (links)."""
    value = value.strip()
    if "/" in value:
        return datetime.strptime(value, "%d/%m/%Y").date()
    return date.fromisoformat(value)


def _filtered_checkins():
    args = request.args
    query = Checkin.query
    try:
        d_from = _parse_day(args.get("from")) if args.get("from") else date.today()
        d_to = _parse_day(args.get("to")) if args.get("to") else d_from
    except ValueError:
        d_from = d_to = date.today()
    start, _ = _day_bounds(d_from)
    _, end = _day_bounds(d_to)
    query = query.filter(Checkin.timestamp >= start, Checkin.timestamp < end)
    if args.get("user"):
        query = query.filter(Checkin.user_id == args["user"])
    if args.get("relay", "").isdigit():
        query = query.filter(Checkin.relay_id == int(args["relay"]))
    if args.get("status") in ("OK", "KO"):
        query = query.filter(Checkin.status == args["status"])
    if args.get("face") == "flagged":
        query = query.filter(Checkin.flagged_filter())
    elif args.get("face") == "match":
        query = query.filter(Checkin.verify_status == "match")
    elif args.get("face") == "unchecked":
        query = query.filter((Checkin.verify_status.is_(None))
                             | (Checkin.verify_status.in_(("no_reference", "legacy"))))
    if args.get("area") in ("outside", "inside", "no_location"):
        query = query.filter(Checkin.area_check == args["area"])
    return query.order_by(Checkin.timestamp.desc()), d_from, d_to


@admin_bp.get("/checkins")
@login_required
def checkins():
    query, d_from, d_to = _filtered_checkins()
    return render_template("checkins.html", checkins=query.limit(1000).all(), d_from=d_from, d_to=d_to,
                           relays=Relay.query.order_by(Relay.name).all(), args=request.args)


def _csv_cell(value):
    """Spreadsheets run cells starting with = + - @ as formulas: prefix them so they stay text."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@admin_bp.get("/checkins.csv")
@login_required
def checkins_csv():
    query, d_from, d_to = _filtered_checkins()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "timezone", "user_id", "full_name", "relay", "direction", "status", "reason",
                "distance", "face_check", "face_similarity", "time_check", "latitude", "longitude",
                "location_accuracy_m", "area_check", "distance_from_site_m"])
    for c in query.all():
        w.writerow([_csv_cell(v) for v in (
            c.local_timestamp.strftime("%d/%m/%Y %H:%M:%S"), c.timezone or "", c.user_id, c.user.full_name,
            c.relay_label, c.direction, c.status, c.reason or "",
            f"{c.distance:.3f}" if c.distance is not None else "",
            c.verify_status or "pending", f"{c.verify_score:.3f}" if c.verify_score is not None else "",
            c.time_check or "ok",
            f"{c.latitude:.6f}" if c.latitude is not None else "",
            f"{c.longitude:.6f}" if c.longitude is not None else "",
            f"{c.location_accuracy:.0f}" if c.location_accuracy is not None else "",
            c.area_check or "", f"{c.area_distance:.0f}" if c.area_distance is not None else "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=checkins_{d_from:%d-%m-%Y}_{d_to:%d-%m-%Y}.csv"})

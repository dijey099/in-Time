"""Admin web UI: collaborators, companies, relays, settings, check-ins, logs."""
import csv
import io
import json
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, send_from_directory, session, url_for)

from urllib.parse import urlsplit

from . import accounts, roles
from .accounts import can, forbidden, permission_required
from . import lockout
from .auth import client_ip, csrf_protect, csrf_token
from .face_engine import NoFace
from .models import AuditLog, Checkin, Company, Department, Relay, Setting, Collaborator, db, hash_token, tz_from_name
from . import audit, verify

admin_bp = Blueprint("admin", __name__)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PHOTO_EXT = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png"}


@admin_bp.before_request
def _csrf():
    csrf_protect()


@admin_bp.before_request
def _current_user():
    """Who is logged in, with which role: read from the database at every request, so a role change,
    a deactivated account or a deleted one applies at once."""
    if not accounts.load_current_user():
        session.clear()
        flash("Your session has ended (account changed, disabled or removed): log in again.", "error")
        return redirect(url_for("admin.login"))


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
            "source_url": current_app.config.get("SOURCE_URL"), "can": can, "current_user": g.get("user"),
            "current_role": g.get("role"), "role_label": roles.label,
            "roles_choices": roles.choices(),
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
        typed = request.form.get("username", "").strip()[:100] or "(empty)"
        lockout.purge_old()
        target = accounts.login_target(request.form.get("username", ""))
        refused = lockout.blocked(target)
        if refused:  # locked account (or paused): not even the right password is checked
            db.session.rollback()
            current_app.logger.warning("Console login refused for %s from %s: account locked/paused", typed, ip)
            audit.log("auth.login_locked", actor=typed, details={"account": target.key}, commit=True)
            flash(refused, "error")
            return render_template("login.html"), 423
        principal, error = accounts.authenticate(request.form.get("username", ""), request.form.get("password", ""))
        if principal is not None:
            accounts.keep_seen_groups()
            lockout.clear(target.key)
            lockout.clear(accounts.normalize(principal.username))
            db.session.commit()  # last login, directory details, account created at first login
            accounts.start_session(principal)
            accounts.load_current_user()
            audit.log("auth.login", "user", principal.id, principal.full_name or principal.username,
                      {"source": accounts.SOURCES.get(principal.source, "Built-in (main.conf)"), "role": g.role},
                      commit=True)
            return redirect(_safe_next(request.args.get("next", "")) or url_for(accounts.home_endpoint()))
        db.session.rollback()
        accounts.keep_seen_groups()  # directory groups of someone without a role yet: shown on Groups
        message, reason = error
        current_app.logger.warning("Failed console login for %s from %s (%s)", typed, ip, reason)
        audit.log("auth.login_failed", actor=typed, details={"reason": reason} if reason else None, commit=True)
        if not accounts.not_a_guess(reason):  # only wrong passwords count towards the lock
            target = accounts.login_target(request.form.get("username", ""))  # fresh after the rollback
            result = lockout.record_failure(target, ip)
            if result:
                n, minutes = current_app.config["LOGIN_MAX_FAILURES"], current_app.config["LOGIN_LOCK_MINUTES"]
                current_app.logger.error("Console account %s %s after %d wrong passwords", target.key, result, n)
                audit.log("auth.account_locked", "user", target.user.id if target.user else None, target.key,
                          {"wrong_passwords": n, "lock": "until unlocked by an administrator" if result == "locked"
                           else f"paused {minutes} min"}, actor=typed, commit=True)
                if result == "locked":
                    message = "Too many wrong passwords: this account is now locked. Ask an administrator to unlock it"
        flash(message, "error")
    return render_template("login.html")


@admin_bp.post("/logout")
def logout():
    if session.get("admin"):
        audit.log("auth.logout", commit=True)
    session.clear()
    return redirect(url_for("admin.login"))


# ------------------------------------------------------------------ dashboard
def _day_bounds(d: date):
    # Local day of the server
    start = datetime.combine(d, time.min).astimezone()
    return start, start + timedelta(days=1)


@admin_bp.get("/")
@permission_required("dashboard.view")
def dashboard():
    start, end = _day_bounds(date.today())
    today = (Checkin.query.filter(Checkin.timestamp >= start, Checkin.timestamp < end)
             .order_by(Checkin.timestamp.desc()).all())
    stats = {
        "collaborators": Collaborator.query.filter_by(active=True).count(),
        "missing_face": Collaborator.query.filter(Collaborator.active.is_(True), Collaborator.face_embedding.is_(None)).count(),
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


# ---------------------------------------------------------------------- collaborators
@admin_bp.get("/collaborators")
@permission_required("collaborators.view")
def collaborators():
    q = request.args.get("q", "").strip()
    company = request.args.get("company", "")
    dept = request.args.get("department", "")
    query = Collaborator.query
    if q:
        like = f"%{q}%"
        query = query.filter((Collaborator.id.ilike(like)) | (Collaborator.full_name.ilike(like)))
    if company == "__none__":
        query = query.filter((Collaborator.company_id.is_(None)) | (Collaborator.department_id.is_(None)))
    elif company.isdigit():
        query = query.filter(Collaborator.company_id == int(company))
    if dept.isdigit():
        query = query.filter(Collaborator.department_id == int(dept))
    # collaborators without a company or department (created before companies existed)
    invalid = Collaborator.query.filter((Collaborator.company_id.is_(None)) | (Collaborator.department_id.is_(None))).count()
    return render_template("collaborators.html", collaborators=query.order_by(Collaborator.id).all(), q=q, company=company, dept=dept,
                           companies=Company.query.order_by(Company.name).all(), invalid_count=invalid)


def _photo_ext(file_storage):
    ext = PHOTO_EXT.get(("." + file_storage.filename.rsplit(".", 1)[-1]).lower()) if "." in file_storage.filename else None
    if not ext:
        raise ValueError("Photo must be a .jpg, .jpeg or .png file")
    return ext


def _save_photo(collaborator: Collaborator, data: bytes, ext: str):
    photos_dir = current_app.config["PHOTOS_DIR"]
    for old in (".jpg", ".png"):  # drop previous photo with another extension
        (photos_dir / f"{collaborator.id}{old}").unlink(missing_ok=True)
    name = f"{collaborator.id}{ext}"
    (photos_dir / name).write_bytes(data)
    collaborator.photo_path = f"photos/{name}"


def _parse_descriptor(raw):
    if not raw:
        return None
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != 128:
        raise ValueError("Invalid face descriptor")
    return [float(v) for v in values]


@admin_bp.route("/collaborators/new", methods=["GET", "POST"])
@admin_bp.route("/collaborators/<collaborator_id>/edit", methods=["GET", "POST"])
@permission_required("collaborators.edit")
def collaborator_form(collaborator_id=None):
    collaborator = db.session.get(Collaborator, collaborator_id) if collaborator_id else None
    if collaborator_id and not collaborator:
        abort(404)
    before = audit.snapshot(collaborator, COLLABORATOR_AUDIT) if collaborator else {}

    if request.method == "POST":
        try:
            if collaborator is None:
                new_id = request.form.get("id", "").strip()
                if not ID_RE.match(new_id):
                    raise ValueError("ID may only contain letters, digits, '-' and '_'")
                if db.session.get(Collaborator, new_id):
                    raise ValueError(f"Collaborator {new_id} already exists")
                collaborator = Collaborator(id=new_id)
                db.session.add(collaborator)
            full_name = request.form.get("full_name", "").strip()
            if not full_name:
                raise ValueError("Full name is required")
            collaborator.full_name = full_name
            company, department = _form_company_department()
            collaborator.company_id, collaborator.department_id = company.id, department.id
            collaborator.active = request.form.get("active") == "on"

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
                _save_photo(collaborator, data, ext)
                collaborator.descriptor = descriptor
                collaborator.face_embedding = embedding
                collaborator.face_model = verify.MODEL_ID if verify.engine().ok else None  # else: done by the worker
            after = audit.snapshot(collaborator, COLLABORATOR_AUDIT)
            diff = audit.changes(before, after)
            if photo and photo.filename and before:  # a new photo replaces the previous one
                diff["photo"] = [before.get("photo"), "new photo"]
            if collaborator_id is None:
                audit.log("collaborator.create", "collaborator", collaborator.id, collaborator.full_name, after)
            elif diff:
                audit.log("collaborator.update", "collaborator", collaborator.id, collaborator.full_name, diff)
            db.session.commit()
            flash(f"Collaborator {collaborator.id} saved", "ok")
            return redirect(url_for("admin.collaborators"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            if collaborator_id is None:
                collaborator = None

    return render_template("collaborator_form.html", collaborator=collaborator, companies=Company.query.order_by(Company.name).all())


# Fields recorded in the audit log when a collaborator is created or edited
COLLABORATOR_AUDIT = (
    "full_name",
    ("company", lambda c: db.session.get(Company, c.company_id).name if c.company_id else None),
    ("department", lambda c: db.session.get(Department, c.department_id).name if c.department_id else None),
    "active",
    ("photo", lambda c: "yes" if c.photo_path else "no"),
)


def _form_company_department():
    """Company chosen in the collaborator form, and a department of that company."""
    raw_company = request.form.get("company_id", "").strip()
    raw_dept = request.form.get("department_id", "").strip()
    company = db.session.get(Company, int(raw_company)) if raw_company.isdigit() else None
    if company is None:
        if not Company.query.first():
            raise ValueError("No company yet: create a company and its departments on the Companies page first")
        raise ValueError("Please choose the collaborator's company")
    department = db.session.get(Department, int(raw_dept)) if raw_dept.isdigit() else None
    if department is None or department.company_id != company.id:
        if not company.departments:
            raise ValueError(f"{company.name} has no department yet: add one on the Companies page")
        raise ValueError(f"Please choose a department of {company.name}")
    return company, department


@admin_bp.post("/collaborators/check-faces")
@permission_required("collaborators.edit")
def collaborators_check_faces():
    """Compute the server face data of photos not processed yet."""
    if not verify.engine().ok:
        flash(verify.engine().message, "error")
        return redirect(url_for("admin.collaborators"))
    done, failed = 0, []
    for u in verify.collaborators_to_process():
        try:
            verify.compute_collaborator_face(u)
            done += 1
        except (NoFace, OSError) as exc:
            u.face_model = verify.MODEL_ID
            failed.append(f"{u.id} ({exc})")
        db.session.commit()
    audit.log("collaborator.check_faces", details={"processed": done, "no_usable_face": failed}, commit=True)
    flash(f"{done} photo(s) processed" + (f"; no usable face: {', '.join(failed)}" if failed else ""),
          "error" if failed else "ok")
    return redirect(url_for("admin.collaborators"))


@admin_bp.post("/collaborators/<collaborator_id>/delete")
@permission_required("collaborators.edit")
def collaborator_delete(collaborator_id):
    collaborator = db.session.get(Collaborator, collaborator_id) or abort(404)
    if Checkin.query.filter_by(collaborator_id=collaborator.id).first():
        if collaborator.active:
            audit.log("collaborator.deactivate", "collaborator", collaborator.id, collaborator.full_name,
                      {"active": [True, False]})
        collaborator.active = False
        flash(f"Collaborator {collaborator.id} has check-ins: deactivated instead of deleted", "ok")
    else:
        if collaborator.photo_path:
            (current_app.config["PHOTOS_DIR"] / collaborator.photo_path.split("/")[-1]).unlink(missing_ok=True)
        audit.log("collaborator.delete", "collaborator", collaborator.id, collaborator.full_name,
                  audit.snapshot(collaborator, COLLABORATOR_AUDIT))
        db.session.delete(collaborator)
        flash(f"Collaborator {collaborator.id} deleted", "ok")
    db.session.commit()
    return redirect(url_for("admin.collaborators"))


@admin_bp.get("/photos/<path:name>")
@permission_required("collaborators.view")
def photo(name):
    return send_from_directory(current_app.config["PHOTOS_DIR"], name)


@admin_bp.get("/snapshots/<path:name>")
@permission_required("attendances.view")
def snapshot(name):
    return send_from_directory(current_app.config["SNAPSHOTS_DIR"], name)


# ------------------------------------------------------------------ companies
NAME_MAX = 100


def _clean_name(value, what):
    name = " ".join((value or "").split())
    if not name:
        raise ValueError(f"{what} name is required")
    if len(name) > NAME_MAX:
        raise ValueError(f"{what} name must be at most {NAME_MAX} characters")
    return name


def _department_names(raw):
    """Department names typed one per line or comma separated (duplicates ignored)."""
    names = [_clean_name(n, "Department") for n in re.split(r"[,\n]", raw or "") if n.strip()]
    return list(dict.fromkeys(names))


def _check_company_name(name, company=None):
    other = Company.query.filter(db.func.lower(Company.name) == name.lower()).first()
    if other and other is not company:
        raise ValueError(f"A company named {other.name} already exists")


def _add_departments(company, names):
    existing = {d.name.lower() for d in company.departments}
    added = []
    for name in names:
        if name.lower() not in existing:
            company.departments.append(Department(name=name))
            existing.add(name.lower())
            added.append(name)
    return added


@admin_bp.route("/companies", methods=["GET", "POST"])
@permission_required("companies.view")
def companies():
    if request.method == "POST" and not can("companies.edit"):
        return forbidden("companies.edit")
    if request.method == "POST":  # new company
        try:
            name = _clean_name(request.form.get("name"), "Company")
            _check_company_name(name)
            company = Company(name=name)
            db.session.add(company)
            _add_departments(company, _department_names(request.form.get("departments")))
            db.session.flush()  # company id for the log
            audit.log("company.create", "company", company.id, company.name,
                      {"departments": [d.name for d in company.departments]})
            db.session.commit()
            flash(f"Company {company.name} created"
                  + ("" if company.departments else ": add its departments below"), "ok")
            return redirect(url_for("admin.companies", _anchor=f"company-{company.id}"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    company_collaborators = dict(db.session.query(Collaborator.company_id, db.func.count(Collaborator.id))
                         .filter(Collaborator.company_id.isnot(None)).group_by(Collaborator.company_id).all())
    dept_collaborators = dict(db.session.query(Collaborator.department_id, db.func.count(Collaborator.id))
                      .filter(Collaborator.department_id.isnot(None)).group_by(Collaborator.department_id).all())
    return render_template("companies.html", companies=Company.query.order_by(Company.name).all(),
                           company_collaborators=company_collaborators, dept_collaborators=dept_collaborators, form=request.form)


@admin_bp.post("/companies/<int:company_id>/edit")
@permission_required("companies.edit")
def company_edit(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    try:
        name = _clean_name(request.form.get("name"), "Company")
        _check_company_name(name, company)
        old = company.name
        company.name = name
        if old != name:
            audit.log("company.rename", "company", company.id, name, {"name": [old, name]})
        db.session.commit()
        flash(f"Company renamed to {company.name}", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.companies", _anchor=f"company-{company_id}"))


@admin_bp.post("/companies/<int:company_id>/delete")
@permission_required("companies.edit")
def company_delete(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    count = Collaborator.query.filter_by(company_id=company.id).count()
    if count:
        flash(f"{company.name} has {count} collaborator(s): move them to another company first", "error")
        return redirect(url_for("admin.companies", _anchor=f"company-{company_id}"))
    audit.log("company.delete", "company", company.id, company.name,
              {"departments": [d.name for d in company.departments]})
    db.session.delete(company)  # and its departments
    db.session.commit()
    flash(f"Company {company.name} deleted", "ok")
    return redirect(url_for("admin.companies"))


@admin_bp.post("/companies/<int:company_id>/departments")
@permission_required("companies.edit")
def department_add(company_id):
    company = db.session.get(Company, company_id) or abort(404)
    try:
        names = _department_names(request.form.get("names"))
        if not names:
            raise ValueError("Department name is required")
        added = _add_departments(company, names)
        if not added:
            raise ValueError(f"{company.name} already has {'this department' if len(names) == 1 else 'these departments'}")
        db.session.flush()
        for d in company.departments:
            if d.name in added:
                audit.log("department.create", "department", d.id, d.name, {"company": company.name})
        db.session.commit()
        flash(f"Added to {company.name}: {', '.join(added)}", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.companies", _anchor=f"company-{company_id}"))


@admin_bp.post("/departments/<int:department_id>/edit")
@permission_required("companies.edit")
def department_edit(department_id):
    dept = db.session.get(Department, department_id) or abort(404)
    try:
        name = _clean_name(request.form.get("name"), "Department")
        if any(d is not dept and d.name.lower() == name.lower() for d in dept.company.departments):
            raise ValueError(f"{dept.company.name} already has a department named {name}")
        old = dept.name
        dept.name = name
        if old != name:
            audit.log("department.rename", "department", dept.id, name,
                      {"company": dept.company.name, "name": [old, name]})
        db.session.commit()
        flash(f"Department renamed to {dept.name}", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.companies", _anchor=f"company-{dept.company_id}"))


@admin_bp.post("/departments/<int:department_id>/delete")
@permission_required("companies.edit")
def department_delete(department_id):
    dept = db.session.get(Department, department_id) or abort(404)
    company_id = dept.company_id
    count = Collaborator.query.filter_by(department_id=dept.id).count()
    if count:
        flash(f"{dept.name} has {count} collaborator(s): move them to another department first", "error")
    else:
        audit.log("department.delete", "department", dept.id, dept.name, {"company": dept.company.name})
        db.session.delete(dept)
        db.session.commit()
        flash(f"Department {dept.name} deleted", "ok")
    return redirect(url_for("admin.companies", _anchor=f"company-{company_id}"))


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


# Fields recorded in the audit log when a relay is created or edited
RELAY_AUDIT = ("name", "allowed_ips", "max_in_time", "min_out_time", "timezone", "enabled", "local_relay",
               ("area", lambda r: f"{r.area_lat}, {r.area_lng} ({r.area_radius:g} m)" if r.has_area else None))


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
@permission_required("relays.manage")
def relays():
    if request.method == "POST":
        token = Relay.new_token()
        relay = Relay(token_hash=hash_token(token))
        try:
            _validate_relay_form(relay, new=True)
            if Relay.query.filter_by(name=relay.name).first():
                raise ValueError("A relay with this name already exists")
            db.session.add(relay)
            db.session.flush()
            audit.log("relay.create", "relay", relay.id, relay.name, audit.snapshot(relay, RELAY_AUDIT))
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
@permission_required("relays.manage")
def relay_edit(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    before = audit.snapshot(relay, RELAY_AUDIT)
    try:
        _validate_relay_form(relay)
        diff = audit.changes(before, audit.snapshot(relay, RELAY_AUDIT))
        if diff:
            audit.log("relay.update", "relay", relay.id, relay.name, diff)
        db.session.commit()
        flash(f"Relay {relay.name} updated", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.relays"))


@admin_bp.post("/relays/<int:relay_id>/token")
@permission_required("relays.manage")
def relay_token(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    token = Relay.new_token()
    relay.token_hash = hash_token(token)
    relay.instance_id = None  # free the one-relay binding: a new/replacement relay can use the token
    relay.public_key = None
    audit.log("relay.new_token", "relay", relay.id, relay.name)  # the token itself is never logged
    db.session.commit()
    session["new_token"] = {"name": relay.name, "token": token}
    return redirect(url_for("admin.relays"))


@admin_bp.post("/relays/<int:relay_id>/delete")
@permission_required("relays.manage")
def relay_delete(relay_id):
    relay = db.session.get(Relay, relay_id) or abort(404)
    if Checkin.query.filter_by(relay_id=relay.id).first():
        if relay.enabled:
            audit.log("relay.disable", "relay", relay.id, relay.name, {"enabled": [True, False]})
        relay.enabled = False
        flash(f"Relay {relay.name} has check-ins: disabled instead of deleted", "ok")
    else:
        audit.log("relay.delete", "relay", relay.id, relay.name, audit.snapshot(relay, RELAY_AUDIT))
        db.session.delete(relay)
        flash(f"Relay {relay.name} deleted", "ok")
    db.session.commit()
    return redirect(url_for("admin.relays"))


# ------------------------------------------------------------------- settings
@admin_bp.route("/settings", methods=["GET", "POST"])
@permission_required("settings.general")
def settings():
    if request.method == "POST":
        try:
            before = {k: Setting.get(k) for k in Setting.DEFAULTS}
            for key in Setting.DEFAULTS:
                value = request.form.get(key, "").strip()
                if not TIME_RE.match(value):
                    raise ValueError(f"{key} must be HH:MM")
                Setting.set(key, value)
            diff = audit.changes(before, {k: Setting.get(k) for k in Setting.DEFAULTS})
            if diff:
                audit.log("settings.update", "settings", None, "Global settings", diff)
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
    if args.get("collaborator"):
        query = query.filter(Checkin.collaborator_id == args["collaborator"])
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


@admin_bp.get("/attendances")
@permission_required("attendances.view")
def attendances():
    query, d_from, d_to = _filtered_checkins()
    return render_template("checkins.html", checkins=query.limit(1000).all(), d_from=d_from, d_to=d_to,
                           relays=Relay.query.order_by(Relay.name).all(), args=request.args)


@admin_bp.get("/checkins")
@admin_bp.get("/checkins.csv")
@admin_bp.get("/attendance")
@admin_bp.get("/attendance.csv")
def old_checkins_url():
    """Former addresses of the Attendances page (bookmarks): same filters, new address."""
    endpoint = "admin.attendances_csv" if request.path.endswith(".csv") else "admin.attendances"
    return redirect(url_for(endpoint, **request.args), 301)


def _csv_cell(value):
    """Spreadsheets run cells starting with = + - @ as formulas: prefix them so they stay text."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@admin_bp.get("/attendances.csv")
@permission_required("attendances.export")
def attendances_csv():
    query, d_from, d_to = _filtered_checkins()
    rows = query.all()
    audit.log("checkins.export", details={"filters": {k: v for k, v in request.args.items() if v},
                                          "from": d_from.isoformat(), "to": d_to.isoformat(), "rows": len(rows)},
              commit=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "timezone", "collaborator_id", "full_name", "company", "department", "relay", "direction", "status", "reason",
                "distance", "face_check", "face_similarity", "time_check", "latitude", "longitude",
                "location_accuracy_m", "area_check", "distance_from_site_m"])
    for c in rows:
        w.writerow([_csv_cell(v) for v in (
            c.local_timestamp.strftime("%d/%m/%Y %H:%M:%S"), c.timezone or "", c.collaborator_id, c.collaborator.full_name,
            c.collaborator.company.name if c.collaborator.company else "", c.collaborator.department.name if c.collaborator.department else "",
            c.relay_label, c.direction, c.status, c.reason or "",
            f"{c.distance:.3f}" if c.distance is not None else "",
            c.verify_status or "pending", f"{c.verify_score:.3f}" if c.verify_score is not None else "",
            c.time_check or "ok",
            f"{c.latitude:.6f}" if c.latitude is not None else "",
            f"{c.longitude:.6f}" if c.longitude is not None else "",
            f"{c.location_accuracy:.0f}" if c.location_accuracy is not None else "",
            c.area_check or "", f"{c.area_distance:.0f}" if c.area_distance is not None else "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=attendances_{d_from:%d-%m-%Y}_{d_to:%d-%m-%Y}.csv"})


# ----------------------------------------------------------------------- logs
LOGS_PER_PAGE = 100


@admin_bp.app_template_filter("audit_label")
def _audit_label(action):
    return audit.label(action)


@admin_bp.app_template_filter("audit_value")
def _audit_value(value):
    """A logged value for display: lists joined, empty values shown as "—"."""
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _filtered_logs():
    args = request.args
    query = AuditLog.query
    try:
        d_from = _parse_day(args.get("from")) if args.get("from") else date.today() - timedelta(days=29)
        d_to = _parse_day(args.get("to")) if args.get("to") else date.today()
    except ValueError:
        d_from, d_to = date.today() - timedelta(days=29), date.today()
    start, _ = _day_bounds(d_from)
    _, end = _day_bounds(d_to)
    query = query.filter(AuditLog.at >= start, AuditLog.at < end)
    if args.get("actor"):
        query = query.filter(AuditLog.actor == args["actor"])
    action = args.get("action", "")
    if action.startswith("cat:"):  # a whole category (e.g. every Companies action)
        names = [a for a, (cat, _) in audit.ACTIONS.items() if cat == action[4:]]
        query = query.filter(AuditLog.action.in_(names))
    elif action:
        query = query.filter(AuditLog.action == action)
    if args.get("q", "").strip():
        like = f"%{args['q'].strip()}%"
        query = query.filter(AuditLog.target_id.ilike(like) | AuditLog.target_label.ilike(like)
                             | AuditLog.ip.ilike(like))
    return query.order_by(AuditLog.at.desc(), AuditLog.id.desc()), d_from, d_to


@admin_bp.get("/logs")
@permission_required("logs.view")
def logs():
    query, d_from, d_to = _filtered_logs()
    page = max(1, request.args.get("page", 1, type=int) or 1)
    total = query.count()
    rows = query.offset((page - 1) * LOGS_PER_PAGE).limit(LOGS_PER_PAGE).all()
    categories = {}
    for name, (cat, label) in audit.ACTIONS.items():
        categories.setdefault(cat, []).append((name, label))
    actors = [a for (a,) in db.session.query(AuditLog.actor).distinct().order_by(AuditLog.actor)]
    args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("logs.html", logs=rows, total=total, page=page, per_page=LOGS_PER_PAGE,
                           d_from=d_from, d_to=d_to, categories=categories, actors=actors, args=args)


@admin_bp.get("/logs.csv")
@permission_required("logs.view")
def logs_csv():
    query, d_from, d_to = _filtered_logs()
    rows = query.all()
    audit.log("logs.export", details={"filters": {k: v for k, v in request.args.items() if v and k != "page"},
                                      "from": d_from.isoformat(), "to": d_to.isoformat(), "rows": len(rows)},
              commit=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user", "ip", "action", "description", "target_type", "target_id", "target", "details"])
    for e in rows:
        w.writerow([_csv_cell(v) for v in (
            e.at.astimezone().strftime("%d/%m/%Y %H:%M:%S"), e.actor, e.ip or "", e.action, audit.label(e.action),
            e.target_type or "", e.target_id or "", e.target_label or "",
            json.dumps(e.details, ensure_ascii=False) if e.details else "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=logs_{d_from:%d-%m-%Y}_{d_to:%d-%m-%Y}.csv"})

from . import access_admin  # noqa: E402,F401  (Settings > Authentication / Users / Groups / Roles, account page)

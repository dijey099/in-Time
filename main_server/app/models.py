import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


_OFFSET_RE = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")


def tz_from_name(name):
    """IANA name ("Indian/Antananarivo") or fixed offset ("UTC+03:00") -> tzinfo, else None."""
    if not name:
        return None
    m = _OFFSET_RE.match(name)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Collaborator(db.Model):
    __tablename__ = "collaborators"

    id = db.Column(db.String(64), primary_key=True)  # employee / badge ID
    full_name = db.Column(db.String(200), nullable=False)
    # Company and department (one of that company's departments), managed on the Companies page.
    # Required in the admin form; empty only for collaborators created before companies existed.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), index=True)
    # Department name from the old DEPARTMENTS setting in .env (before companies). No longer used:
    # copied into a company at the first start of this version (see migrate.py).
    legacy_department = db.Column("department", db.String(100), index=True)
    photo_path = db.Column(db.String(300))  # e.g. "photos/E001.jpg"
    # 128 floats computed by face-api.js in the admin's browser (JSON list, preview only)
    descriptor = db.Column(db.JSON)
    # 128 floats computed on the server (OpenCV SFace) from the photo: reference for the face check.
    # face_model set and face_embedding empty = photo checked, no usable face.
    face_embedding = db.Column(db.JSON(none_as_null=True))
    face_model = db.Column(db.String(32))
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    company = db.relationship("Company")
    department = db.relationship("Department")

    def to_api(self):
        return {
            "id": self.id,
            "full_name": self.full_name,
            "has_photo": bool(self.photo_path),
            "descriptor": self.descriptor,
            "active": self.active,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    departments = db.relationship("Department", back_populates="company", order_by="Department.name",
                                  cascade="all, delete-orphan")


class Department(db.Model):
    __tablename__ = "departments"
    __table_args__ = (db.UniqueConstraint("company_id", "name", name="uq_departments_company_name"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    company = db.relationship("Company", back_populates="departments")


class Relay(db.Model):
    __tablename__ = "relays"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)  # e.g. "SITE A"
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    # Comma separated list of IPs / CIDR ranges allowed to use the token
    allowed_ips = db.Column(db.String(500), nullable=False, default="")
    # Optional per-site overrides of the global settings ("HH:MM")
    max_in_time = db.Column(db.String(5))
    min_out_time = db.Column(db.String(5))
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    # On the same network as the main server: connects directly, without a reverse proxy.
    # Its IP is the address that opened the connection (TRUSTED_PROXIES / CF_PROXY are ignored).
    local_relay = db.Column(db.Boolean, nullable=False, default=False)
    last_seen = db.Column(db.DateTime(timezone=True))
    last_ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    # Machine ID of the relay that first used this token: a token belongs to one relay server.
    instance_id = db.Column(db.String(64))
    # Ed25519 public key of that relay server (base64), recorded with its first request. Every later
    # request and check-in must be signed with the matching private key. Reset with a new token.
    public_key = db.Column(db.String(64))
    # Time zone of the site (IANA name or "UTC+03:00"). Late/early is computed in this zone, never in
    # one sent by the relay. Empty: set from the relay's first check-in (and editable on the Relays page).
    timezone = db.Column(db.String(64))
    # Start of the current connected period (no gap > 5 min between requests): check-ins made after
    # it should arrive within seconds; a long delay means the relay held them back (flagged "delayed").
    online_since = db.Column(db.DateTime(timezone=True))
    # Highest check-in sequence number received from this relay, and the relay key it belongs to.
    # The relay numbers its check-ins 1, 2, 3... and signs them: a missing number means check-ins
    # were deleted on the relay before they reached this server (flagged "gap").
    last_seq = db.Column(db.Integer)
    seq_key = db.Column(db.String(64))
    # Site area picked on a map (circle: centre + radius in metres). A check-in whose GPS location is
    # outside it is still accepted but flagged "outside" on the dashboard. Empty: no area check.
    area_lat = db.Column(db.Float)
    area_lng = db.Column(db.Float)
    area_radius = db.Column(db.Float)

    @property
    def has_area(self):
        return self.area_lat is not None and self.area_lng is not None and bool(self.area_radius)

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(500), nullable=False)

    DEFAULTS = {
        "max_in_time": "08:00",   # check-in after this time is "late" (KO)
        "min_out_time": "17:00",  # check-out before this time is "early" (KO)
    }

    @classmethod
    def get(cls, key):
        row = db.session.get(cls, key)
        return row.value if row else cls.DEFAULTS.get(key)

    @classmethod
    def set(cls, key, value):
        row = db.session.get(cls, key)
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))


class Checkin(db.Model):
    __tablename__ = "checkins"

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False)  # generated by relay
    collaborator_id = db.Column(db.String(64), db.ForeignKey("collaborators.id"), nullable=False, index=True)
    relay_id = db.Column(db.Integer, db.ForeignKey("relays.id"), nullable=False, index=True)
    direction = db.Column(db.String(3), nullable=False)  # IN / OUT
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    status = db.Column(db.String(2), nullable=False)  # OK / KO
    reason = db.Column(db.String(50))  # late / early / ...
    distance = db.Column(db.Float)  # face match distance (lower = better)
    snapshot_path = db.Column(db.String(300))
    received_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    # Time zone of the site when the check was made (IANA name or "UTC+03:00")
    timezone = db.Column(db.String(64))
    # Name of the relay that made the check (kept even if the relay is renamed later)
    relay_name = db.Column(db.String(100))
    # Server-side face check of the snapshot against the collaborator's photo (see verify.py)
    verify_status = db.Column(db.String(12), index=True)  # None = not checked yet
    verify_score = db.Column(db.Float)  # cosine similarity snapshot / photo
    verified_at = db.Column(db.DateTime(timezone=True))
    # SHA-256 of the snapshot: the same picture used twice is a replay
    snapshot_sha256 = db.Column(db.String(64), index=True)
    # Time check: None = ok; "delayed" = received long after it was made while the relay was connected
    time_check = db.Column(db.String(12))
    # OK/KO sent by the relay, when it differs from the one computed here (for review)
    relay_status = db.Column(db.String(2))
    # Sequence number given by the relay (signed with the check-in)
    relay_seq = db.Column(db.Integer)
    # GPS location of the phone when the check-in was made (signed by the relay)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    location_accuracy = db.Column(db.Float)  # metres, as reported by the phone
    # Location vs the relay's area when received: "inside" / "outside" / "no_location" (relay has an
    # area but sent no location: older relay version). None = the relay had no area set.
    area_check = db.Column(db.String(12), index=True)
    area_distance = db.Column(db.Float)  # metres from the area's centre

    FLAGGED = ("mismatch", "no_face", "no_snapshot", "replay")
    TIME_FLAGGED = ("delayed", "gap")

    @property
    def flagged(self):
        return self.verify_status in self.FLAGGED or self.time_check in self.TIME_FLAGGED

    @classmethod
    def flagged_filter(cls):
        return cls.verify_status.in_(cls.FLAGGED) | cls.time_check.in_(cls.TIME_FLAGGED)

    collaborator = db.relationship("Collaborator")
    relay = db.relationship("Relay")

    @property
    def local_timestamp(self):
        """Time as seen on the site (its time zone); server time zone if unknown."""
        tz = tz_from_name(self.timezone)
        return self.timestamp.astimezone(tz) if tz else self.timestamp.astimezone()

    @property
    def outside_area(self):
        return self.area_check == "outside"

    @property
    def area_excess(self):
        """Metres beyond the edge of the relay's area (for display)."""
        r = self.relay
        if self.area_distance is None or not r or not r.area_radius:
            return None
        return max(0.0, self.area_distance - r.area_radius)

    @property
    def relay_label(self):
        return self.relay_name or (self.relay.name if self.relay else "")


class LoginFailure(db.Model):
    """Wrong passwords typed at the console login, per account (see lockout.py)."""
    __tablename__ = "login_failures"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), index=True)  # account tried (lower-case)
    ip = db.Column(db.String(64), nullable=False, index=True)
    at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class AuditLog(db.Model):
    """Every action made in the admin console: who, when, from where, on what, and what changed.
    Append-only: the console has no way to edit or delete these rows (see audit.py)."""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    # Login name of the console account at the time of the action (kept if the account is renamed or
    # removed later). For a failed login: the name that was typed.
    actor = db.Column(db.String(100), nullable=False, index=True)
    ip = db.Column(db.String(64))
    action = db.Column(db.String(40), nullable=False, index=True)  # see audit.ACTIONS
    target_type = db.Column(db.String(30))  # collaborator / company / department / relay / settings / ...
    target_id = db.Column(db.String(100), index=True)
    target_label = db.Column(db.String(200))  # name of the target at the time (e.g. company name)
    # What changed ({"field": [old, new]}) and other facts (filters of an export, number of rows...)
    details = db.Column(db.JSON)


# ------------------------------------------------- console accounts and access
user_groups = db.Table(
    "user_groups",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class User(db.Model):
    """Account that can log in to the admin console (not to be confused with Collaborator, the people
    who check in). Its role is the highest of its own role and those of its groups (see accounts.py)."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)  # stored lower-case
    full_name = db.Column(db.String(200))
    email = db.Column(db.String(200))
    # Where the password is checked: "local" (hash below), "ldap" or "ad" (directory server)
    source = db.Column(db.String(10), nullable=False, default="local")
    password_hash = db.Column(db.String(300))  # local accounts only
    role = db.Column(db.String(20))  # own role (roles.ROLES), None = only the roles of its groups
    active = db.Column(db.Boolean, nullable=False, default=True)
    # LDAP / AD: the account's DN and the directory groups it belonged to at its last login
    directory_dn = db.Column(db.String(500))
    directory_groups = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_login_at = db.Column(db.DateTime(timezone=True))
    last_login_ip = db.Column(db.String(64))
    # Locked after too many wrong passwords: no login until an Administrator unlocks it (lockout.py)
    locked_at = db.Column(db.DateTime(timezone=True))
    # LDAP / AD accounts read from the directory: last time seen there, and disabled there (AD)
    synced_at = db.Column(db.DateTime(timezone=True))
    directory_disabled = db.Column(db.Boolean, nullable=False, default=False)

    groups = db.relationship("Group", secondary=user_groups, back_populates="users", order_by="Group.name")


class Group(db.Model):
    """Group of console users with a role.

    - source "local": created on the Groups page, members added by hand (local accounts).
    - source "ldap" / "ad": a group of the directory, read from the server (directory_group = its DN).
      Its members are the directory's: an account in it gets the group's role at login. No role = the
      group gives nothing."""
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(10), nullable=False, default="local", index=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    description = db.Column(db.String(300))
    role = db.Column(db.String(20))
    directory_group = db.Column(db.String(500))  # DN of the directory group
    synced_at = db.Column(db.DateTime(timezone=True))  # last time it was read from the directory
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    users = db.relationship("User", secondary=user_groups, back_populates="groups", order_by="User.username")

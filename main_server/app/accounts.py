"""Console accounts: login (local, LDAP, Active Directory, built-in administrator), the user of the
current request, roles and permissions.

- Built-in administrator: username / password in [admin] of main.conf. Always an Administrator and
  always able to log in: it can't be turned off, disabled or locked from the console (repeated wrong
  passwords only pause it for a few minutes, see lockout.py).
- Local accounts: password hash stored here (werkzeug scrypt).
- LDAP / Active Directory accounts: the password is checked by the directory server at each login,
  and the account's directory groups are refreshed. An account that logs in for the first time is
  created automatically (when allowed) if it gets a role from a group.

A user's role is the highest of its own role, its groups' roles and the roles of the groups linked to
the directory groups it belongs to. It is read from the database at every request, so a change
(role, group, deactivation) applies at once.
"""
import base64
import hashlib
import hmac
from dataclasses import dataclass
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import roles
from .directory import AuthFailed, DirectoryError, cn_of
from .models import Group, Setting, User, db, utcnow

SOURCES = {"local": "Local", "ldap": "LDAP", "ad": "Active Directory"}
PASSWORD_MIN = 12

# ------------------------------------------------------------ auth settings
AUTH_DEFAULTS = {
    # The one source passwords are checked against: "local" (default, nothing to set up), "ldap" or "ad"
    "auth.source": "local",
    "auth.ldap.url": "", "auth.ldap.start_tls": "0", "auth.ldap.verify_cert": "1",
    "auth.ldap.bind_dn": "", "auth.ldap.bind_password": "", "auth.ldap.user_base": "",
    "auth.ldap.user_filter": "(uid={username})", "auth.ldap.attr_name": "cn", "auth.ldap.attr_email": "mail",
    "auth.ldap.group_base": "", "auth.ldap.group_filter": "(|(member={dn})(uniqueMember={dn})(memberUid={username}))",
    "auth.ldap.auto_create": "1",
    "auth.ad.url": "", "auth.ad.bind_user": "", "auth.ad.bind_password": "", "auth.ad.group_base": "",
    "auth.ad.user_base": "", "auth.ad.domain": "", "auth.ad.base_dn": "", "auth.ad.start_tls": "0",
    "auth.ad.verify_cert": "1", "auth.ad.nested_groups": "1", "auth.ad.auto_create": "1",
}
BOOL_KEYS = {k for k, v in AUTH_DEFAULTS.items() if v in ("0", "1") and not k.endswith("password")}
SECRET_KEYS = {"auth.ldap.bind_password", "auth.ad.bind_password"}


def auth_setting(key):
    row = db.session.get(Setting, key)
    return row.value if row else AUTH_DEFAULTS[key]


def auth_flag(key):
    return auth_setting(key) == "1"


def auth_source():
    """The authentication source in use: "local", "ldap" or "ad" (only one at a time)."""
    source = auth_setting("auth.source")
    return source if source in SOURCES else "local"


def _fernet():
    key = hashlib.sha256(b"in-Time directory secrets\n" + current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def unseal(value):
    """Decrypted secret; None if it can't be read (SECRET_KEY changed): enter it again."""
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def directory_conf(source, values=None):
    """Settings of "ldap" or "ad" as a dict for directory.py (secrets decrypted). `values`: settings
    not saved yet (to test them), else the saved ones."""
    prefix = f"auth.{source}."
    conf = {}
    for key in AUTH_DEFAULTS:
        if key.startswith(prefix):
            name, value = key[len(prefix):], (values[key] if values is not None else auth_setting(key))
            if key in SECRET_KEYS:
                value = unseal(value) or ""
            elif key in BOOL_KEYS:
                value = value == "1"
            conf[name] = value
    return conf


# ----------------------------------------------------------------- principals
@dataclass
class BuiltinAdmin:
    """The account of main.conf ([admin] username / password)."""
    username: str
    id = None
    source = "env"
    full_name = "Built-in administrator (main.conf)"
    email = None
    active = True


def group_matches(group, directory_groups):
    """The group is linked to one of the account's directory groups (by DN or by name)."""
    wanted = (group.directory_group or "").strip().lower()
    if not wanted:
        return False
    return any(wanted == str(dn).lower() or wanted == str(cn_of(dn)).lower() for dn in directory_groups or [])


def directory_mapped_groups(user, with_role_only=True):
    """Directory groups (of the account's source) the account belonged to at its last login."""
    if user.source == "local" or not user.directory_groups:
        return []
    query = Group.query.filter(Group.source == user.source, Group.directory_group.isnot(None))
    if with_role_only:
        query = query.filter(Group.role.isnot(None))
    return [grp for grp in query.order_by(Group.name).all() if group_matches(grp, user.directory_groups)]


def all_groups(user):
    """Groups that give the account a role: its local groups, or its directory groups that have a role."""
    if isinstance(user, BuiltinAdmin):
        return []
    if user.source == "local":
        return [grp for grp in user.groups if grp.role]
    return directory_mapped_groups(user)


# ----------------------------------------------------------- directory groups
GROUP_SYNC_MINUTES = 10  # the Groups page reads the directory again after this long


def _sync_key(source):
    return f"auth.groups_synced.{source}"


def last_group_sync(source):
    row = db.session.get(Setting, _sync_key(source))
    if not row or not row.value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def sync_directory_groups(source):
    """Read the groups of the directory and add / update them on the Groups page (their roles are kept).
    Returns (added, updated, gone). DirectoryError if the directory can't be read."""
    from . import directory
    found = directory.list_groups(source, directory_conf(source))
    now = utcnow()
    existing = Group.query.filter_by(source=source).all()
    by_dn = {(g.directory_group or "").lower(): g for g in existing}
    by_name = {(g.directory_group or g.name or "").lower(): g for g in existing}
    added = updated = 0
    for item in found:
        grp = by_dn.get(item["dn"].lower()) or by_name.get(item["name"].lower())
        if grp is None:
            grp = Group(source=source, name=item["name"][:200], directory_group=item["dn"][:500],
                        description=item["description"], synced_at=now)
            db.session.add(grp)
            by_dn[item["dn"].lower()] = grp
            added += 1
            continue
        if (grp.name, grp.directory_group, grp.description) != (item["name"][:200], item["dn"][:500], item["description"]):
            updated += 1
        grp.name, grp.directory_group, grp.description = item["name"][:200], item["dn"][:500], item["description"]
        grp.synced_at = now
    gone = sum(1 for g in existing if g.synced_at is None or g.synced_at < now)
    return {"groups_added": added, "groups_updated": updated, "groups_gone": gone, "_found": found, "_now": now}


def _user_group_dns(source, conf, user, groups):
    """Directory groups of an account, from the listing (logins read them again exactly)."""
    if source == "ad":
        dns = list(user["member_of"])
        if conf.get("nested_groups", True):  # groups inside groups
            parents = {g["dn"].lower(): g["member_of"] for g in groups}
            seen, todo = {d.lower() for d in dns}, list(dns)
            while todo:
                for parent in parents.get(todo.pop().lower(), []):
                    if parent.lower() not in seen:
                        seen.add(parent.lower())
                        dns.append(parent)
                        todo.append(parent)
        return dns
    if user.get("exact"):  # searched with the LDAP group filter, like at login
        return list(user["member_of"])
    dn, name = user["dn"].lower(), user["username"].lower()
    return [g["dn"] for g in groups
            if dn in {m.lower() for m in g["members"]} or name in {m.lower() for m in g["member_uids"]}]


def sync_directory(source):
    """Read the groups and the accounts of the directory: groups appear on the Groups page (their roles
    are kept) and accounts on the Users page (with their groups). Returns counts. DirectoryError if the
    directory can't be read."""
    from . import directory
    result = sync_directory_groups(source)
    found_groups, now = result.pop("_found"), result.pop("_now")
    conf = directory_conf(source)
    found = directory.list_users(source, conf)
    existing = {u.username.lower(): u for u in User.query.all()}
    added = updated = conflicts = 0
    for item in found:
        username = normalize(item["username"])
        user = existing.get(username)
        groups = _user_group_dns(source, conf, item, found_groups)
        if user is None:
            if item["disabled"]:
                continue  # disabled in the directory: not added
            user = User(username=username, source=source, active=True)
            db.session.add(user)
            existing[username] = user
            added += 1
        elif user.source != source:
            conflicts += 1  # a local (or other source) account has this name: left as it is
            continue
        elif (user.full_name, user.email, user.directory_dn, user.directory_groups, user.directory_disabled) != \
                (item["full_name"], item["email"], item["dn"], groups, item["disabled"]):
            updated += 1
        if item["full_name"]:
            user.full_name = str(item["full_name"])[:200]
        if item["email"]:
            user.email = str(item["email"])[:200]
        user.directory_dn, user.directory_groups = item["dn"][:500], groups
        user.directory_disabled, user.synced_at = item["disabled"], now
    gone = User.query.filter(User.source == source,
                             db.or_(User.synced_at.is_(None), User.synced_at < now)).count()
    Setting.set(_sync_key(source), now.isoformat())
    result.update(users_added=added, users_updated=updated, users_gone=gone, users_conflicts=conflicts)
    return result


def add_seen_groups(source, dns):
    """Directory groups an account belongs to, added to the Groups page if missing (without a role):
    groups appear even when the directory can't be listed, and before anyone has a role."""
    if source == "local" or not dns:
        return
    known = {(g.directory_group or "").lower() for g in Group.query.filter_by(source=source).all()}
    for dn in dns:
        if str(dn).lower() not in known:
            db.session.add(Group(source=source, name=str(cn_of(dn))[:200], directory_group=str(dn)[:500]))
            known.add(str(dn).lower())


def keep_seen_groups():
    """After a refused login (no role yet): still record the directory groups that were seen."""
    seen = g.pop("seen_groups", None)
    if seen:
        add_seen_groups(*seen)
        db.session.commit()


def effective_role(user):
    if isinstance(user, BuiltinAdmin):
        return "administrator"
    return roles.highest([user.role] + [grp.role for grp in all_groups(user)])


def has_permission(user, perm):
    return perm in roles.permissions(effective_role(user))


# --------------------------------------------------------------------- login
def normalize(username):
    return (username or "").strip().lower()[:100]


def _directory_sources():
    source = auth_source()
    return [] if source == "local" else [source]


def _directory_login(source, name, password):
    from . import directory
    return directory.authenticate(source, directory_conf(source), name, password)


def find_user(typed):
    """Console account for what was typed at login ("EXAMPLE\\alice" / "alice@domain" for AD)."""
    user = User.query.filter(db.func.lower(User.username) == normalize(typed)).first()
    if user is None and "ad" in _directory_sources():
        from .directory import ad_username
        user = User.query.filter(db.func.lower(User.username) == normalize(ad_username(typed, {})),
                                 User.source == "ad").first()
    return user


def login_target(username):
    """Which account a login attempt is for (its wrong passwords are counted under it)."""
    from .lockout import Target
    typed = (username or "").strip()
    if hmac.compare_digest(typed, current_app.config["ADMIN_USERNAME"]):
        return Target("builtin", normalize(typed))
    user = find_user(typed)
    if user is not None:
        return Target("user", user.username, user)
    if "ad" in _directory_sources() and ("\\" in typed or "@" in typed):
        from .directory import ad_username
        return Target("unknown", normalize(ad_username(typed, {})))
    return Target("unknown", normalize(typed) or "(empty)")


def authenticate(username, password):
    """(principal, None) or (None, (message for the person, reason for the log))."""
    typed = (username or "").strip()
    cfg = current_app.config
    bad = ("Invalid credentials", None)
    if hmac.compare_digest(typed, cfg["ADMIN_USERNAME"]):
        if hmac.compare_digest(password or "", cfg["ADMIN_PASSWORD"]):
            return BuiltinAdmin(cfg["ADMIN_USERNAME"]), None
        return None, (bad[0], "wrong password (built-in administrator)")

    user = find_user(typed)

    if user is not None:
        if not user.active:
            return None, ("This account is disabled", "account disabled")
        if user.source != auth_source():  # only accounts of the source in use can log in
            return None, (f"{SOURCES[user.source]} accounts can't log in: the console uses "
                          f"{SOURCES[auth_source()]} authentication", f"{user.source} login turned off")
        if user.source == "local":
            if not user.password_hash or not check_password_hash(user.password_hash, password or ""):
                return None, (bad[0], "wrong password")
        else:
            try:
                info = _directory_login(user.source, typed if user.source == "ad" else user.username, password)
            except AuthFailed as exc:
                return None, (bad[0], f"{user.source}: {exc}")
            except DirectoryError as exc:
                current_app.logger.error("%s server error: %s", SOURCES[user.source], exc)
                return None, ("The directory server can't be reached: try again later", f"{user.source} server: {exc}")
            _apply_directory_info(user, info)
    else:
        # Unknown account: try the directory server, which may create accounts at first login
        user, reasons = None, []
        for source in _directory_sources():
            if not auth_flag(f"auth.{source}.auto_create"):
                continue
            try:
                info = _directory_login(source, typed, password)
            except AuthFailed as exc:
                reasons.append(f"{source}: {exc}")
                continue
            except DirectoryError as exc:
                current_app.logger.error("%s server error: %s", SOURCES[source], exc)
                reasons.append(f"{source} server: {exc}")
                continue
            from .directory import ad_username
            user = User(username=normalize(ad_username(typed, {}) if source == "ad" else typed), source=source,
                        active=True)
            _apply_directory_info(user, info)
            if effective_role(user) is None:
                return None, ("Your account has no role in in-Time: ask an administrator to add you to a group",
                              f"{source}: no group with a role")
            db.session.add(user)
            db.session.flush()
            break
        if user is None:
            return None, (bad[0], "; ".join(reasons) or "unknown user")

    if effective_role(user) is None:
        return None, ("Your account has no role in in-Time: ask an administrator to give you one", "no role")
    user.last_login_at = utcnow()
    from .auth import client_ip
    user.last_login_ip = (client_ip() or request.remote_addr or "")[:64] or None
    return user, None


def not_a_guess(reason):
    """Failed logins that are not password guesses (the password was right, or wasn't checked): they
    don't count towards the admin lock, so someone without a role can't lock everyone out."""
    reason = reason or ""
    return (reason in ("no role", "account disabled") or reason.endswith("no group with a role")
            or "login turned off" in reason or " server: " in reason)


def _apply_directory_info(user, info):
    user.directory_dn = info.get("dn")
    user.directory_groups = info.get("groups") or []
    g.seen_groups = (user.source, list(user.directory_groups))
    if info.get("full_name"):
        user.full_name = str(info["full_name"])[:200]
    if info.get("email"):
        user.email = str(info["email"])[:200]


def start_session(principal):
    session.clear()
    session["admin"] = True  # logged in
    session["uid"] = principal.id
    session["builtin"] = isinstance(principal, BuiltinAdmin)
    session["username"] = principal.username


# ------------------------------------------------------- current request user
def load_current_user():
    """g.user / g.role / g.perms for a logged-in session. False if the session is no longer valid."""
    g.user, g.role, g.perms = None, None, frozenset()
    if not session.get("admin"):
        return True
    if session.get("builtin") or ("uid" not in session):  # sessions from before accounts existed
        if session.get("username", current_app.config["ADMIN_USERNAME"]) != current_app.config["ADMIN_USERNAME"]:
            return False
        g.user = BuiltinAdmin(current_app.config["ADMIN_USERNAME"])
    else:
        user = db.session.get(User, session.get("uid"))
        if user is None or not user.active:
            return False
        g.user = user
    g.role = effective_role(g.user)
    if g.role is None:
        return False
    g.perms = roles.permissions(g.role)
    return True


def can(perm):
    return perm in getattr(g, "perms", frozenset())


def permission_required(*perms):
    """Logged in and allowed to do one of `perms` (403 page otherwise, recorded in the logs)."""
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin") or g.get("user") is None:
                return redirect(url_for("admin.login", next=request.full_path.rstrip("?")))
            if perms and not any(can(p) for p in perms):
                return forbidden(*perms)
            return view(*args, **kwargs)
        return wrapper
    return deco


def forbidden(*perms):
    """403 page for an action the user's role doesn't allow (recorded in the logs)."""
    from . import audit
    audit.log("auth.access_denied", details={"page": request.path, "method": request.method,
                                             "role": g.role, "needs": list(perms)}, commit=True)
    return render_template("forbidden.html", needed=perms), 403


def home_endpoint():
    """First page the user may open."""
    for perm, endpoint in (("dashboard.view", "admin.dashboard"), ("logs.view", "admin.logs")):
        if can(perm):
            return endpoint
    return "admin.account"


# --------------------------------------------------------------- safeguards
def check_admin_access_kept():
    """Called before committing a change to accounts or groups: the person making it keeps their own
    Administrator access (the built-in administrator of main.conf always keeps it)."""
    db.session.flush()
    me = g.get("user")
    if isinstance(me, User):
        db.session.refresh(me)
        if not me.active or me.locked_at or effective_role(me) != "administrator":
            raise ValueError("This change would remove your own Administrator access: ask another "
                             "administrator to make it")


def check_password_policy(password, username=""):
    if len(password or "") < PASSWORD_MIN:
        raise ValueError(f"The password must have at least {PASSWORD_MIN} characters")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("The password must contain letters and digits")
    if username and username.lower() in password.lower():
        raise ValueError("The password must not contain the username")


def set_password(user, password):
    check_password_policy(password, user.username)
    user.password_hash = generate_password_hash(password)

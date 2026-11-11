"""Settings > Authentication, Users, Groups and Roles (Administrator), and the account page (everyone)."""
import re

from flask import current_app, flash, g, redirect, render_template, request, url_for

from . import accounts, audit, roles
from .accounts import AUTH_DEFAULTS, SOURCES, permission_required
from .admin import admin_bp
from .directory import AuthFailed, DirectoryError, cn_of, test_connection
from .models import Group, Setting, User, db

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\\-]{1,100}$")
URL_RE = re.compile(r"^ldaps?://[^\s/]+(:\d+)?/?$", re.I)


def _group_names(groups):
    return sorted(grp.name for grp in groups)


# ------------------------------------------------------------- authentication
# Each saved password and the account it belongs to: emptying the account removes the password
SECRET_ACCOUNT = {"auth.ldap.bind_password": "auth_ldap_bind_dn", "auth.ad.bind_password": "auth_ad_bind_user"}


def _auth_form_values():
    """Every authentication setting from the form. A password left empty keeps the saved one; it is
    removed when its account (bind DN / reader account) is emptied."""
    new = {}
    for key in AUTH_DEFAULTS:
        field = key.replace(".", "_")
        if key in accounts.SECRET_KEYS:
            typed = request.form.get(field, "")
            if not request.form.get(SECRET_ACCOUNT[key], "").strip():
                new[key] = ""  # no account: no password to keep
            elif typed:
                new[key] = accounts.seal(typed)
            else:
                new[key] = accounts.auth_setting(key)  # empty = keep the current one
        elif key in accounts.BOOL_KEYS:
            new[key] = "1" if request.form.get(field) == "on" else "0"
        elif key.endswith("filter"):
            new[key] = request.form.get(field, "").strip()
        else:
            new[key] = " ".join(request.form.get(field, "").split())
        if len(new[key]) > 500:
            raise ValueError(f"{key.split('.')[-1]} is too long")
    if new["auth.source"] not in SOURCES:
        raise ValueError("Choose the authentication source")
    return new


def _check_directory_settings(source, new):
    if not URL_RE.match(new[f"auth.{source}.url"]):
        raise ValueError("Server URL must look like ldap://server:389 or ldaps://server:636")
    if source == "ldap":
        if not new["auth.ldap.user_base"]:
            raise ValueError("LDAP: the users base DN is required")
        if "{username}" not in new["auth.ldap.user_filter"]:
            raise ValueError("LDAP: the user filter must contain {username}")
    if source == "ad" and not re.match(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", new["auth.ad.domain"]):
        raise ValueError("Active Directory: enter the domain name, e.g. example.local")


def _save_auth(new):
    source = new["auth.source"]
    if source != "local":
        _check_directory_settings(source, new)
    before = {k: accounts.auth_setting(k) for k in AUTH_DEFAULTS}
    for key, value in new.items():
        if value != before[key] or db.session.get(Setting, key) is not None:
            Setting.set(key, value)
    diff = {}
    for key in new:
        if new[key] == before[key]:
            continue
        name = "source" if key == "auth.source" else key.split(".", 1)[1]
        if key in accounts.SECRET_KEYS:
            diff[name] = ["(hidden)", "changed"]
        elif key in accounts.BOOL_KEYS:
            diff[name] = [before[key] == "1", new[key] == "1"]
        elif key == "auth.source":
            diff[name] = [SOURCES.get(before[key], before[key]), SOURCES[new[key]]]
        else:
            diff[name] = [before[key], new[key]]
    if diff:
        audit.log("auth.settings_update", "settings", "authentication", "Authentication", diff)
    accounts.check_admin_access_kept()
    return diff


@admin_bp.route("/settings/authentication", methods=["GET", "POST"])
@permission_required("access.manage")
def settings_authentication():
    test_result = None
    form_values = None
    if request.method == "POST":
        action = request.form.get("action", "save")
        try:
            new = _auth_form_values()
            if action == "save":
                diff = _save_auth(new)
                db.session.commit()
                flash("Authentication settings saved" if diff else "Nothing changed", "ok")
                if diff and new["auth.source"] != "local":
                    _sync_groups(new["auth.source"])  # its groups, ready on the Groups page
                return redirect(url_for("admin.settings_authentication"))
            if action in ("test-ldap", "test-ad"):  # the values of the form, not saved
                source = action[5:]
                form_values = new
                _check_directory_settings(source, new)
                username = request.form.get(f"test_username_{source}", "").strip()
                try:
                    message = test_connection(source, accounts.directory_conf(source, new), username or None,
                                              request.form.get(f"test_password_{source}", ""))
                    test_result = (source, "ok", message)
                except (AuthFailed, DirectoryError) as exc:
                    kind = "Login refused" if isinstance(exc, AuthFailed) else "Server error"
                    test_result = (source, "error", f"{kind}: {exc}")
                audit.log("auth.test", "settings", source, SOURCES[source],
                          {"test_user": username or None, "result": test_result[1], "message": test_result[2][:300]},
                          commit=True)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            form_values = form_values or _safe_form_values()
    saved_secret = accounts.auth_setting("auth.ldap.bind_password")
    values = dict(form_values) if form_values else {k: accounts.auth_setting(k) for k in AUTH_DEFAULTS}
    values["auth.ldap.bind_password"] = bool(values["auth.ldap.bind_password"])  # never sent to the page
    counts = {s: User.query.filter_by(source=s, active=True).count() for s in SOURCES}
    return render_template("settings_authentication.html", v=values, test_result=test_result,
                           saved_source=accounts.auth_source(), counts=counts,
                           admin_username=current_app.config["ADMIN_USERNAME"],
                           secret_unreadable=bool(saved_secret) and accounts.unseal(saved_secret) is None,
                           max_failures=current_app.config["LOGIN_MAX_FAILURES"],
                           lock_minutes=current_app.config["LOGIN_LOCK_MINUTES"])


def _safe_form_values():
    """What was typed, to show the form again after an error (secrets never shown)."""
    values = {k: accounts.auth_setting(k) for k in AUTH_DEFAULTS}
    for key in AUTH_DEFAULTS:
        field = key.replace(".", "_")
        if key in accounts.SECRET_KEYS:
            continue
        if key in accounts.BOOL_KEYS:
            values[key] = "1" if request.form.get(field) == "on" else "0"
        elif field in request.form:
            values[key] = request.form.get(field, "")
    return values


# ---------------------------------------------------------------------- users
def _user_audit(user):
    return {"full_name": user.full_name, "email": user.email, "source": SOURCES.get(user.source),
            "role": roles.label(user.role) if user.role else None, "groups": _group_names(user.groups),
            "active": user.active}


@admin_bp.get("/settings/users")
@permission_required("access.manage")
def settings_users():
    source = accounts.auth_source()
    last = _sync_if_stale(source) if source != "local" else None  # LDAP / AD: accounts read from the directory
    # only the accounts of the authentication source in use (the others can't log in)
    users = User.query.filter_by(source=source).order_by(User.username).all()
    return render_template("settings_users.html", users=users, effective=accounts.effective_role,
                           auth_source=source, last_sync=last, q=request.args.get("q", ""),
                           all_groups=accounts.all_groups, in_directory=_in_directory,
                           admin_username=current_app.config["ADMIN_USERNAME"], sources=SOURCES)


@admin_bp.post("/settings/users/sync")
@permission_required("access.manage")
def settings_users_sync():
    if accounts.auth_source() != "local":
        _sync_groups(accounts.auth_source())
    return redirect(url_for("admin.settings_users"))


def _in_directory(user):
    """The account was found the last time the directory was read (directory accounts only)."""
    last = accounts.last_group_sync(user.source)
    return bool(user.synced_at and last and user.synced_at >= last)


@admin_bp.route("/settings/users/new", methods=["GET", "POST"])
@admin_bp.route("/settings/users/<int:user_id>/edit", methods=["GET", "POST"])
@permission_required("access.manage")
def settings_user_form(user_id=None):
    user = db.session.get(User, user_id) if user_id else None
    if user_id and user is None:
        return redirect(url_for("admin.settings_users"))
    groups = Group.query.filter_by(source="local").order_by(Group.name).all()
    if request.method == "POST":
        before = _user_audit(user) if user else None
        try:
            if user is None:
                username = accounts.normalize(request.form.get("username"))
                if not USERNAME_RE.match(username):
                    raise ValueError("Username: letters, digits and . _ @ \\ - only (up to 100)")
                if username == accounts.normalize(current_app.config["ADMIN_USERNAME"]):
                    raise ValueError("This username is the built-in administrator's ([admin] username in main.conf)")
                if User.query.filter(db.func.lower(User.username) == username).first():
                    raise ValueError(f"A user named {username} already exists")
                user = User(username=username, source=accounts.auth_source())  # the source in use
                db.session.add(user)
            user.full_name = " ".join(request.form.get("full_name", "").split())[:200] or None
            user.email = request.form.get("email", "").strip()[:200] or None
            role = request.form.get("role") or None
            if role and role not in roles.ROLES:
                raise ValueError("Unknown role")
            user.role = role
            if user.source == "local":  # directory accounts: groups come from the directory
                user.groups = [grp for grp in groups if str(grp.id) in request.form.getlist("groups")]
            user.active = request.form.get("active") == "on"
            password = request.form.get("password", "")
            reset = False
            if user.source == "local":
                if password or user_id is None:
                    if password != request.form.get("password2", ""):
                        raise ValueError("The two passwords are different")
                    accounts.set_password(user, password)
                    reset = user_id is not None
            if user.source == "local" and accounts.effective_role(user) is None and user.active:
                raise ValueError("Give this user a role, or a group that has one (otherwise they can't log in)")
            db.session.flush()
            after = _user_audit(user)
            if before is None:
                audit.log("user.create", "user", user.id, user.username, after)
            else:
                diff = audit.changes(before, after)
                if diff:
                    audit.log("user.update", "user", user.id, user.username, diff)
                if reset:
                    audit.log("user.password_reset", "user", user.id, user.username)  # never the password
            accounts.check_admin_access_kept()
            db.session.commit()
            flash(f"User {user.username} saved", "ok")
            return redirect(url_for("admin.settings_users"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            user = db.session.get(User, user_id) if user_id else None
    mapped = accounts.directory_mapped_groups(user, with_role_only=False) if user else []
    return render_template("settings_user_form.html", user=user, groups=groups, mapped=mapped, sources=SOURCES,
                           auth_source=accounts.auth_source(),
                           effective=accounts.effective_role(user) if user else None, cn_of=cn_of,
                           password_min=accounts.PASSWORD_MIN)


@admin_bp.post("/settings/users/<int:user_id>/delete")
@permission_required("access.manage")
def settings_user_delete(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        return redirect(url_for("admin.settings_users"))
    try:
        if isinstance(g.user, User) and g.user.id == user.id:
            raise ValueError("You can't delete your own account")
        if user.source != "local" and _in_directory(user):
            raise ValueError(f"{user.username} is still in the directory (it would come back): disable it instead")
        audit.log("user.delete", "user", user.id, user.username, _user_audit(user))
        db.session.delete(user)
        accounts.check_admin_access_kept()
        db.session.commit()
        flash(f"User {user.username} deleted", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.settings_users"))


@admin_bp.post("/settings/users/<int:user_id>/unlock")
@permission_required("access.manage")
def settings_user_unlock(user_id):
    from . import lockout
    user = db.session.get(User, user_id)
    if user is not None and lockout.unlock(user):
        audit.log("user.unlock", "user", user.id, user.username)
        db.session.commit()
        flash(f"User {user.username} unlocked", "ok")
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/settings/users/") else url_for("admin.settings_users"))


# --------------------------------------------------------------------- groups
def _group_form(group):
    name = " ".join(request.form.get("name", "").split())
    if not name or len(name) > 100:
        raise ValueError("Group name is required (up to 100 characters)")
    other = Group.query.filter(Group.source == "local", db.func.lower(Group.name) == name.lower()).first()
    if other and other is not group:
        raise ValueError(f"A group named {other.name} already exists")
    role = request.form.get("role", "")
    if role not in roles.ROLES:
        raise ValueError("Choose the group's role")
    group.name, group.role = name, role
    group.description = " ".join(request.form.get("description", "").split())[:300] or None


def _group_audit(group):
    return {"name": group.name, "description": group.description,
            "role": roles.label(group.role) if group.role else None, "directory_group": group.directory_group}


def _sync_groups(source, quiet=False):
    """Read the directory's groups and accounts; flash the result (or the error)."""
    try:
        n = accounts.sync_directory(source)
        db.session.commit()
        if not quiet or n["groups_added"] or n["users_added"]:
            flash(f"Read from {SOURCES[source]}: {n['users_added']} new user(s), {n['users_updated']} updated; "
                  f"{n['groups_added']} new group(s), {n['groups_updated']} updated"
                  + (f"; {n['users_gone']} user(s) and {n['groups_gone']} group(s) no longer in the directory"
                     if n["users_gone"] or n["groups_gone"] else ""), "ok")
        if n["users_conflicts"]:
            flash(f"{n['users_conflicts']} directory account(s) not added: a local account has the same username", "error")
        return True
    except (DirectoryError, AuthFailed) as exc:
        db.session.rollback()
        flash(f"Couldn't read {SOURCES[source]}: {exc}. Accounts and groups seen at logins are still listed.", "error")
        return False


def _sync_if_stale(source):
    from datetime import timedelta
    from .models import utcnow
    last = accounts.last_group_sync(source)
    if last is None or utcnow() - last > timedelta(minutes=accounts.GROUP_SYNC_MINUTES):
        _sync_groups(source, quiet=True)  # read the directory again when the lists are old
    return accounts.last_group_sync(source)


@admin_bp.route("/settings/groups", methods=["GET", "POST"])
@permission_required("access.manage")
def settings_groups():
    source = accounts.auth_source()
    if source != "local":
        return _directory_groups_page(source)
    if request.method == "POST":
        group = Group(source="local")
        try:
            _group_form(group)
            db.session.add(group)
            db.session.flush()
            audit.log("group.create", "group", group.id, group.name, _group_audit(group))
            db.session.commit()
            flash(f"Group {group.name} created", "ok")
            return redirect(url_for("admin.settings_groups", _anchor=f"group-{group.id}"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    groups = Group.query.filter_by(source="local").order_by(Group.name).all()
    return render_template("settings_groups.html", groups=groups, form=request.form)


def _directory_groups_page(source):
    """LDAP / AD: the directory's groups, read from the server; the administrator gives them roles."""
    if request.method == "POST":
        if request.form.get("action") == "sync":
            _sync_groups(source)
            return redirect(url_for("admin.settings_groups"))
        try:  # roles
            changed = 0
            for grp in Group.query.filter_by(source=source).all():
                field = f"role_{grp.id}"
                if field not in request.form:
                    continue
                role = request.form.get(field) or None
                if role and role not in roles.ROLES:
                    raise ValueError("Unknown role")
                if role != grp.role:
                    audit.log("group.update", "group", grp.id, grp.name,
                              {"role": [roles.label(grp.role) if grp.role else None, roles.label(role) if role else None],
                               "directory_group": grp.directory_group})
                    grp.role = role
                    changed += 1
            accounts.check_admin_access_kept()
            db.session.commit()
            flash(f"{changed} group role(s) changed" if changed else "Nothing changed", "ok")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("admin.settings_groups", q=request.form.get("q") or None))
    last = _sync_if_stale(source)
    groups = Group.query.filter_by(source=source).order_by(db.func.lower(Group.name)).all()
    users = User.query.filter_by(source=source).all()
    members = {grp.id: [u.username for u in users if accounts.group_matches(grp, u.directory_groups)] for grp in groups}
    return render_template("settings_groups_directory.html", groups=groups, members=members, source=source,
                           source_label=SOURCES[source], last_sync=last, q=request.args.get("q", ""))


@admin_bp.post("/settings/groups/<int:group_id>/edit")
@permission_required("access.manage")
def settings_group_edit(group_id):
    group = db.session.get(Group, group_id)
    if group is None or group.source != "local":
        return redirect(url_for("admin.settings_groups"))
    before = _group_audit(group)
    try:
        _group_form(group)
        diff = audit.changes(before, _group_audit(group))
        if diff:
            audit.log("group.update", "group", group.id, group.name, diff)
        accounts.check_admin_access_kept()
        db.session.commit()
        flash(f"Group {group.name} saved", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.settings_groups", _anchor=f"group-{group_id}"))


@admin_bp.post("/settings/groups/<int:group_id>/delete")
@permission_required("access.manage")
def settings_group_delete(group_id):
    group = db.session.get(Group, group_id)
    if group is None:
        return redirect(url_for("admin.settings_groups"))
    try:
        last = accounts.last_group_sync(group.source) if group.source != "local" else None
        if group.source != "local" and group.synced_at and last and group.synced_at >= last:
            raise ValueError("This group is still in the directory: remove its role instead")
        audit.log("group.delete", "group", group.id, group.name,
                  {**_group_audit(group), "members": [u.username for u in group.users]})
        db.session.delete(group)
        accounts.check_admin_access_kept()
        db.session.commit()
        flash(f"Group {group.name} deleted", "ok")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.settings_groups"))


# ---------------------------------------------------------------------- roles
@admin_bp.get("/settings/roles")
@permission_required("access.manage")
def settings_roles():
    source = accounts.auth_source()
    users = User.query.filter_by(active=True, source=source).all()
    counts = {r: {"users": 0, "groups": Group.query.filter_by(role=r, source=source).count()} for r in roles.ROLES}
    for u in users:
        role = accounts.effective_role(u)
        if role:
            counts[role]["users"] += 1
    counts["administrator"]["users"] += 1  # the built-in administrator of main.conf
    return render_template("settings_roles.html", roles=roles, counts=counts)


# -------------------------------------------------------------------- account
@admin_bp.route("/account", methods=["GET", "POST"])
@permission_required()
def account():
    user = g.user
    if request.method == "POST":
        try:
            if not isinstance(user, User) or user.source != "local":
                raise ValueError("The password of this account is not managed here")
            from werkzeug.security import check_password_hash
            if not check_password_hash(user.password_hash or "", request.form.get("current", "")):
                raise ValueError("The current password is wrong")
            if request.form.get("password", "") != request.form.get("password2", ""):
                raise ValueError("The two new passwords are different")
            accounts.set_password(user, request.form.get("password", ""))
            audit.log("account.password_change", "user", user.id, user.username)
            db.session.commit()
            flash("Password changed", "ok")
            return redirect(url_for("admin.account"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("account.html", user=user, groups=accounts.all_groups(user), sources=SOURCES,
                           password_min=accounts.PASSWORD_MIN)

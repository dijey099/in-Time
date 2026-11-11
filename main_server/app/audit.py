"""Audit log of the admin console: every action, who made it and what changed.

log() adds the entry to the current database session, so it is saved by the same commit as the
action itself: an action is never recorded without its log entry (or the other way round). Actions
with nothing to commit (exports, logins) call log(..., commit=True).

Who made it ("actor") is the login name of the console account (local, LDAP, Active Directory or
the built-in administrator of main.conf), kept in the session at login.
"""
from flask import current_app, has_request_context, session

from .models import AuditLog, db

# action -> (category, label shown on the Logs page)
ACTIONS = {
    "auth.login": ("Login", "Logged in"),
    "auth.login_failed": ("Login", "Failed login"),
    "auth.login_locked": ("Login", "Login refused (account locked)"),
    "auth.account_locked": ("Login", "Account locked (wrong passwords)"),
    "auth.logout": ("Login", "Logged out"),
    "auth.access_denied": ("Login", "Access denied (role)"),
    "account.password_change": ("Login", "Changed own password"),
    "collaborator.create": ("Collaborators", "Created collaborator"),
    "collaborator.update": ("Collaborators", "Edited collaborator"),
    "collaborator.delete": ("Collaborators", "Deleted collaborator"),
    "collaborator.deactivate": ("Collaborators", "Deactivated collaborator (has check-ins)"),
    "collaborator.check_faces": ("Collaborators", "Checked photos"),
    "company.create": ("Companies", "Created company"),
    "company.rename": ("Companies", "Renamed company"),
    "company.delete": ("Companies", "Deleted company"),
    "department.create": ("Companies", "Added department"),
    "department.rename": ("Companies", "Renamed department"),
    "department.delete": ("Companies", "Deleted department"),
    "relay.create": ("Relays", "Created relay"),
    "relay.update": ("Relays", "Edited relay"),
    "relay.new_token": ("Relays", "Generated new relay token"),
    "relay.delete": ("Relays", "Deleted relay"),
    "relay.disable": ("Relays", "Disabled relay (has check-ins)"),
    "settings.update": ("Settings", "Changed settings"),
    "auth.settings_update": ("Access", "Changed authentication settings"),
    "auth.test": ("Access", "Tested directory connection"),
    "user.create": ("Access", "Created user"),
    "user.update": ("Access", "Edited user"),
    "user.password_reset": ("Access", "Reset user password"),
    "user.unlock": ("Access", "Unlocked user"),
    "user.delete": ("Access", "Deleted user"),
    "group.create": ("Access", "Created group"),
    "group.update": ("Access", "Edited group"),
    "group.delete": ("Access", "Deleted group"),
    "checkins.export": ("Exports", "Exported attendances (CSV)"),
    "logs.export": ("Exports", "Exported logs (CSV)"),
}


def label(action):
    return ACTIONS.get(action, ("", action))[1]


def category(action):
    return ACTIONS.get(action, ("Other", action))[0]


def current_actor():
    """Login name of the person using the console."""
    if has_request_context() and session.get("admin"):
        return session.get("username") or current_app.config["ADMIN_USERNAME"]
    return "system"


def _ip():
    from flask import request
    from .auth import client_ip
    try:
        return (client_ip() or request.remote_addr or "")[:64] or None
    except Exception:  # noqa: BLE001 - never block an action because the address can't be read
        return None


def log(action, target_type=None, target_id=None, target_label=None, details=None, actor=None, commit=False):
    """Record an action (saved with the next commit, or now with commit=True)."""
    assert action in ACTIONS, action
    entry = AuditLog(action=action, actor=(actor or current_actor())[:100], ip=_ip() if has_request_context() else None,
                     target_type=target_type, target_id=None if target_id is None else str(target_id)[:100],
                     target_label=None if target_label is None else str(target_label)[:200],
                     details=details or None)
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def snapshot(obj, fields):
    """Values of `fields` of obj, to compare before/after an edit. A field can be a
    (name, function) pair to record something derived (e.g. the company's name)."""
    out = {}
    for f in fields:
        name, get = (f, lambda o, f=f: getattr(o, f)) if isinstance(f, str) else f
        value = get(obj)
        out[name] = value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
    return out


def changes(before, after):
    """{"field": [old, new]} for the fields that changed."""
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}

"""Per-account login lock of the admin console.

Wrong passwords are counted per account (login_failures, by username). LOGIN_MAX_FAILURES of them for
the same account within LOGIN_LOCK_MINUTES:

- a console account (local, LDAP, Active Directory) is locked: nobody can log in with it, even with
  the right password, until an Administrator unlocks it (Settings > Users) or
  `flask --app main unlock-user <name>` is run;
- the built-in administrator of main.conf is never locked (it must always work): it is paused for
  LOGIN_LOCK_MINUTES, then works again (`flask --app main unlock-admin` ends the pause at once);
- a name that has no account yet (LDAP / AD people before their first login) is paused the same way.

Other accounts are not affected: one person (or an attacker) typing wrong passwords for one account
never blocks anyone else. Failures where the password was right or wasn't checked (no role, disabled
account, directory unreachable) are not counted. A successful login clears the account's count.
"""
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app

from .models import LoginFailure, User, db, utcnow


@dataclass
class Target:
    """The account a login attempt is for."""
    kind: str  # "builtin", "user" or "unknown"
    key: str   # lower-case username the failures are counted under
    user: User | None = None


def _window():
    return timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])


def recent_failures(key):
    return LoginFailure.query.filter(LoginFailure.username == key, LoginFailure.at >= utcnow() - _window())


def blocked(target):
    """Message to show if this account can't log in now, else None."""
    if target.kind == "user" and target.user.locked_at:
        return "This account is locked after too many wrong passwords: ask an administrator to unlock it"
    failures = recent_failures(target.key).order_by(LoginFailure.at).all()
    if target.kind != "user" and len(failures) >= current_app.config["LOGIN_MAX_FAILURES"]:
        until = failures[-current_app.config["LOGIN_MAX_FAILURES"]].at + _window()
        minutes = max(1, int((until - utcnow()).total_seconds() // 60) + 1)
        return f"Too many wrong passwords for this account: try again in {minutes} minute{'s' if minutes > 1 else ''}"
    return None


def record_failure(target, ip):
    """Count a wrong password. Returns "locked" (account just locked), "paused" (built-in administrator
    or unknown name just paused) or None."""
    db.session.add(LoginFailure(username=target.key, ip=(ip or "?")[:64]))
    db.session.flush()
    count = recent_failures(target.key).count()
    result = None
    if count >= current_app.config["LOGIN_MAX_FAILURES"]:
        if target.kind == "user" and not target.user.locked_at:
            target.user.locked_at = utcnow()
            result = "locked"
        elif target.kind != "user" and count == current_app.config["LOGIN_MAX_FAILURES"]:
            result = "paused"
    db.session.commit()
    return result


def clear(key):
    LoginFailure.query.filter(LoginFailure.username == key).delete()


def unlock(user):
    """Unlock a console account. Returns True if it was locked."""
    was = bool(user.locked_at)
    user.locked_at = None
    clear(user.username)
    return was


def purge_old():
    """Failures older than the window no longer count: drop them (called at login)."""
    LoginFailure.query.filter(LoginFailure.at < utcnow() - max(_window(), timedelta(days=1))).delete()

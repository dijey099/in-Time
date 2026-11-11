"""Tiny start-up migrations: add columns introduced after the first release.

db.create_all() creates missing tables but never alters existing ones, so new
columns are added here (idempotent, safe to run at every start).

rename_tables() runs before db.create_all(): tables and columns renamed after a
release are renamed in place, keeping their data.
"""
import logging
import os

from sqlalchemy import inspect, text

log = logging.getLogger("in_time.migrate")

# table -> [(column, SQL type)]
NEW_COLUMNS = {
    "checkins": [("timezone", "VARCHAR(64)"), ("relay_name", "VARCHAR(100)"),
                 ("verify_status", "VARCHAR(12)"), ("verify_score", "DOUBLE PRECISION"),
                 ("verified_at", "TIMESTAMP WITH TIME ZONE"), ("snapshot_sha256", "VARCHAR(64)"),
                 ("time_check", "VARCHAR(12)"), ("relay_status", "VARCHAR(2)"), ("relay_seq", "INTEGER"),
                 ("latitude", "DOUBLE PRECISION"), ("longitude", "DOUBLE PRECISION"),
                 ("location_accuracy", "DOUBLE PRECISION"), ("area_check", "VARCHAR(12)"),
                 ("area_distance", "DOUBLE PRECISION")],
    "collaborators": [("department", "VARCHAR(100)"), ("face_embedding", "JSON"), ("face_model", "VARCHAR(32)"),
              ("company_id", "INTEGER REFERENCES companies(id)"),
              ("department_id", "INTEGER REFERENCES departments(id)")],
    "login_failures": [("username", "VARCHAR(100)")],
    "users": [("locked_at", "TIMESTAMP WITH TIME ZONE"), ("synced_at", "TIMESTAMP WITH TIME ZONE"),
              ("directory_disabled", "BOOLEAN NOT NULL DEFAULT FALSE")],
    "groups": [("source", "VARCHAR(10) NOT NULL DEFAULT 'local'"), ("synced_at", "TIMESTAMP WITH TIME ZONE")],
    "relays": [("local_relay", "BOOLEAN NOT NULL DEFAULT FALSE"), ("instance_id", "VARCHAR(64)"),
               ("timezone", "VARCHAR(64)"), ("online_since", "TIMESTAMP WITH TIME ZONE"),
               ("last_seq", "INTEGER"), ("seq_key", "VARCHAR(64)"), ("public_key", "VARCHAR(64)"),
               ("area_lat", "DOUBLE PRECISION"), ("area_lng", "DOUBLE PRECISION"),
               ("area_radius", "DOUBLE PRECISION")],
}


def _rename(conn, kind, table, old, new):
    """ALTER TABLE ... RENAME CONSTRAINT / ALTER INDEX ... RENAME, when `old` exists."""
    if kind == "constraint":
        if conn.execute(text("SELECT 1 FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND conname = :n"),
                        {"t": table, "n": old}).first():
            conn.execute(text(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"'))
    elif conn.execute(text("SELECT 1 FROM pg_indexes WHERE tablename = :t AND indexname = :n"),
                      {"t": table, "n": old}).first():
        conn.execute(text(f'ALTER INDEX "{old}" RENAME TO "{new}"'))


def _users_to_collaborators(conn, insp):
    """The people who check in were "users" before: table users -> collaborators and
    checkins.user_id -> collaborator_id, with their indexes and constraints (data kept).
    Only a users table of that kind (with full_name and photo_path) is renamed: the name "users"
    is free for other uses afterwards."""
    tables = set(insp.get_table_names())
    if "users" not in tables or "collaborators" in tables:
        return False
    if not {"full_name", "photo_path"} <= {c["name"] for c in insp.get_columns("users")}:
        return False
    conn.execute(text("ALTER TABLE users RENAME TO collaborators"))
    _rename(conn, "constraint", "collaborators", "users_pkey", "collaborators_pkey")
    for col in ("company_id", "department_id"):
        _rename(conn, "constraint", "collaborators", f"users_{col}_fkey", f"collaborators_{col}_fkey")
    for col in ("company_id", "department_id", "department"):
        _rename(conn, "index", "collaborators", f"ix_users_{col}", f"ix_collaborators_{col}")
    if "checkins" in tables and "user_id" in {c["name"] for c in insp.get_columns("checkins")}:
        conn.execute(text("ALTER TABLE checkins RENAME COLUMN user_id TO collaborator_id"))
        _rename(conn, "constraint", "checkins", "checkins_user_id_fkey", "checkins_collaborator_id_fkey")
        _rename(conn, "index", "checkins", "ix_checkins_user_id", "ix_checkins_collaborator_id")
    return True


def rename_tables(db):
    """Renames done before db.create_all() (which would otherwise create the new, empty tables)."""
    with db.engine.begin() as conn:
        if _users_to_collaborators(conn, inspect(conn)):
            log.warning("Database updated: table users renamed to collaborators, "
                        "checkins.user_id renamed to collaborator_id")


DEFAULT_COMPANY = "Default company"


def _seed_default_company(conn):
    """First start with companies: the departments of the old DEPARTMENTS setting (.env) and those
    of existing collaborators go to one company, and collaborators are assigned to it. Rename it on the Companies page."""
    if conn.execute(text("SELECT COUNT(*) FROM companies")).scalar():
        return None
    env = [d.strip() for d in os.environ.get("DEPARTMENTS", "").split(",") if d.strip()]
    used = [r[0].strip() for r in conn.execute(text(
        "SELECT DISTINCT department FROM collaborators WHERE department IS NOT NULL AND department <> ''"))]
    names = list(dict.fromkeys(n[:100] for n in env + used if n.strip()))
    if not names:
        return None
    conn.execute(text("INSERT INTO companies (name, created_at) VALUES (:n, CURRENT_TIMESTAMP)"),
                 {"n": DEFAULT_COMPANY})
    company_id = conn.execute(text("SELECT id FROM companies WHERE name = :n"), {"n": DEFAULT_COMPANY}).scalar()
    for name in names:
        conn.execute(text("INSERT INTO departments (company_id, name, created_at) "
                          "VALUES (:c, :n, CURRENT_TIMESTAMP)"), {"c": company_id, "n": name})
        dept_id = conn.execute(text("SELECT id FROM departments WHERE company_id = :c AND name = :n"),
                               {"c": company_id, "n": name}).scalar()
        conn.execute(text("UPDATE collaborators SET company_id = :c, department_id = :d "
                          "WHERE company_id IS NULL AND TRIM(department) = :n"),
                     {"c": company_id, "d": dept_id, "n": name})
    return names


def _remove_old_lock_file():
    """The console-wide lock file of older versions (admin_login.lock) is replaced by per-account locks."""
    from config import BASE_DIR
    path = BASE_DIR / "admin_login.lock"
    if path.exists():
        try:
            path.unlink()
            log.warning("Removed %s: the console lock is now per account (Settings > Users)", path)
        except OSError as exc:
            log.warning("Could not remove the old lock file %s (%s): it is no longer used", path, exc)


def run(db):
    insp = inspect(db.engine)
    added = []
    with db.engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, sql_type in columns:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                    added.append(f"{table}.{name}")
        if "checkins.relay_name" in added:
            # Existing check-ins: relay name = current name of the relay that sent them.
            # Their time zone was never recorded and stays empty.
            conn.execute(text("UPDATE checkins SET relay_name = "
                              "(SELECT name FROM relays WHERE relays.id = checkins.relay_id) "
                              "WHERE relay_name IS NULL"))
    if "checkins.verify_status" in added:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_checkins_verify_status ON checkins (verify_status)"))
            # Check-ins recorded before the face check existed, without snapshot: not flagged.
            # Those with a snapshot are checked by the background worker.
            conn.execute(text("UPDATE checkins SET verify_status = 'legacy' WHERE snapshot_path IS NULL"))
    if "checkins.snapshot_sha256" in added:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_checkins_snapshot_sha256 ON checkins (snapshot_sha256)"))
    if "collaborators.company_id" in added:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_collaborators_company_id ON collaborators (company_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_collaborators_department_id ON collaborators (department_id)"))
            seeded = _seed_default_company(conn)
        if seeded:
            log.warning("Departments are now managed on the Companies page (the old DEPARTMENTS setting is no longer "
                        "used). Created company %r with department(s) %s and assigned existing collaborators to it.",
                        DEFAULT_COMPANY, ", ".join(seeded))
    if "checkins.area_check" in added:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_checkins_area_check ON checkins (area_check)"))
    if "relays.timezone" in added:
        # Existing relays: pin the time zone of their latest check-in
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE relays SET timezone = (SELECT c.timezone FROM checkins c WHERE c.relay_id = "
                              "relays.id AND c.timezone IS NOT NULL ORDER BY c.id DESC LIMIT 1) "
                              "WHERE timezone IS NULL"))
    # One relay server (machine ID) per relay token: two tokens can't be bound to the same relay
    try:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_relays_instance_id ON relays (instance_id) "
                              "WHERE instance_id IS NOT NULL"))
    except Exception as exc:  # noqa: BLE001 - existing duplicates: the check in auth.py still applies
        log.error("Could not create the unique index on relays.instance_id (%s). Several relay tokens are bound "
                  "to the same relay server: generate a new token for the extra ones on the Relays page.", exc)
    if "login_failures.username" in added:
        # Per-account lock: the old failures were counted for the whole console (no username)
        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM login_failures WHERE username IS NULL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_login_failures_username ON login_failures (username)"))
    with db.engine.begin() as conn:
        # The built-in administrator can no longer be turned off
        conn.execute(text("DELETE FROM settings WHERE key = 'auth.builtin_enabled'"))
        # One authentication source instead of LDAP / AD switches (AD first if both were on)
        old = dict(conn.execute(text("SELECT key, value FROM settings WHERE key IN "
                                     "('auth.ldap.enabled', 'auth.ad.enabled', 'auth.source')")).all())
        if ("auth.ldap.enabled" in old or "auth.ad.enabled" in old) and "auth.source" not in old:
            source = "ad" if old.get("auth.ad.enabled") == "1" else "ldap" if old.get("auth.ldap.enabled") == "1" else "local"
            conn.execute(text("INSERT INTO settings (key, value) VALUES ('auth.source', :v)"), {"v": source})
            log.warning("Authentication source set to %s (one source at a time now)", source)
        conn.execute(text("DELETE FROM settings WHERE key IN ('auth.ldap.enabled', 'auth.ad.enabled')"))
        # The full name is now First name + Last name (two attributes)
        conn.execute(text("DELETE FROM settings WHERE key IN ('auth.ldap.attr_name', 'auth.ad.attr_name')"))
    if "groups.source" in added:
        # Groups now come from the directory: a group linked to a directory group becomes that directory
        # group (matched by DN or name at the next sync); names are unique per source only; no role allowed
        with db.engine.begin() as conn:
            src = conn.execute(text("SELECT value FROM settings WHERE key = 'auth.source'")).scalar()
            conn.execute(text("UPDATE groups SET source = :s WHERE directory_group IS NOT NULL AND directory_group <> ''"),
                         {"s": src if src in ("ldap", "ad") else "ldap"})
            conn.execute(text("ALTER TABLE groups ALTER COLUMN role DROP NOT NULL"))
            conn.execute(text("ALTER TABLE groups ALTER COLUMN name TYPE VARCHAR(200)"))
            conn.execute(text("ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_name_key"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_groups_source ON groups (source)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_groups_name ON groups (name)"))
    _remove_old_lock_file()
    # Removed in the open-source release: the license of the in-Time platform
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS license_state"))
    if added:
        log.warning("Database updated: added column(s) %s", ", ".join(added))

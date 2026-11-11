"""Tiny start-up migrations: add columns introduced after the first release.

db.create_all() creates missing tables but never alters existing ones, so new
columns are added here (idempotent, safe to run at every start).
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
    "users": [("department", "VARCHAR(100)"), ("face_embedding", "JSON"), ("face_model", "VARCHAR(32)"),
              ("company_id", "INTEGER REFERENCES companies(id)"),
              ("department_id", "INTEGER REFERENCES departments(id)")],
    "relays": [("local_relay", "BOOLEAN NOT NULL DEFAULT FALSE"), ("instance_id", "VARCHAR(64)"),
               ("timezone", "VARCHAR(64)"), ("online_since", "TIMESTAMP WITH TIME ZONE"),
               ("last_seq", "INTEGER"), ("seq_key", "VARCHAR(64)"), ("public_key", "VARCHAR(64)"),
               ("area_lat", "DOUBLE PRECISION"), ("area_lng", "DOUBLE PRECISION"),
               ("area_radius", "DOUBLE PRECISION")],
}


DEFAULT_COMPANY = "Default company"


def _seed_default_company(conn):
    """First start with companies: the departments of the old DEPARTMENTS setting (.env) and those
    of existing users go to one company, and users are assigned to it. Rename it on the Companies page."""
    if conn.execute(text("SELECT COUNT(*) FROM companies")).scalar():
        return None
    env = [d.strip() for d in os.environ.get("DEPARTMENTS", "").split(",") if d.strip()]
    used = [r[0].strip() for r in conn.execute(text(
        "SELECT DISTINCT department FROM users WHERE department IS NOT NULL AND department <> ''"))]
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
        conn.execute(text("UPDATE users SET company_id = :c, department_id = :d "
                          "WHERE company_id IS NULL AND TRIM(department) = :n"),
                     {"c": company_id, "d": dept_id, "n": name})
    return names


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
    if "users.company_id" in added:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_company_id ON users (company_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_department_id ON users (department_id)"))
            seeded = _seed_default_company(conn)
        if seeded:
            log.warning("Departments are now managed on the Companies page (DEPARTMENTS in .env is no longer "
                        "used). Created company %r with department(s) %s and assigned existing users to it.",
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
    # Removed in the open-source release: the license of the in-Time platform
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS license_state"))
    if added:
        log.warning("Database updated: added column(s) %s", ", ".join(added))

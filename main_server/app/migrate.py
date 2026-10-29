"""Tiny start-up migrations: add columns introduced after the first release.

db.create_all() creates missing tables but never alters existing ones, so new
columns are added here (idempotent, safe to run at every start).
"""
import logging

from sqlalchemy import inspect, text

log = logging.getLogger("in_time.migrate")

# table -> [(column, SQL type)]
NEW_COLUMNS = {
    "checkins": [("timezone", "VARCHAR(64)"), ("relay_name", "VARCHAR(100)"),
                 ("verify_status", "VARCHAR(12)"), ("verify_score", "DOUBLE PRECISION"),
                 ("verified_at", "TIMESTAMP WITH TIME ZONE")],
    "users": [("department", "VARCHAR(100)"), ("face_embedding", "JSON"), ("face_model", "VARCHAR(32)")],
    "relays": [("local_relay", "BOOLEAN NOT NULL DEFAULT FALSE")],
}


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
    if added:
        log.warning("Database updated: added column(s) %s", ", ".join(added))

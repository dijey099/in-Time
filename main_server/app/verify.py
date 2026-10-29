"""Server-side face check (audit).

- User photos: the server computes each user's reference face data (OpenCV SFace).
- Check-ins: every snapshot sent by a relay is compared with the user's photo.
  Result in Checkin.verify_status:
    match        snapshot and photo show the same person
    mismatch     they don't: flagged
    no_face      no usable face in the snapshot: flagged
    no_snapshot  the relay sent no snapshot: flagged
    no_reference the user has no usable photo: can't be checked (re-checked when it has one)
    legacy       recorded before the face check existed, without snapshot: not checked
    None         not checked yet (models not ready): done by the background worker

Check-ins are always stored: a flagged one is shown on the dashboard for review, never dropped.
"""
import logging
import threading
import time

from flask import current_app
from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from .face_engine import MODEL_ID, FaceEngine, NoFace
from .models import Checkin, User, db, utcnow

log = logging.getLogger("in_time.verify")

WORKER_INTERVAL = 60           # seconds between two background passes
BATCH = 200
_LOCK_KEY = 0x1D7135           # Postgres advisory lock: one worker at a time across gunicorn workers


def engine() -> FaceEngine:
    return current_app.extensions["face_engine"]


# ----------------------------------------------------------------- users
def compute_user_face(user: User):
    """Set user.face_embedding from the photo. Raises NoFace (embedding cleared) or RuntimeError."""
    # keep updated_at: it tells relays to download the photo again
    flag_modified(user, "updated_at")
    user.face_embedding, user.face_model = None, None
    if not user.photo_path:
        return
    path = current_app.config["PHOTOS_DIR"] / user.photo_path.split("/")[-1]
    try:
        emb = engine().embed(path)
    except NoFace:
        user.face_model = MODEL_ID  # checked: no usable face
        raise
    user.face_embedding, user.face_model = engine().to_list(emb), MODEL_ID


def users_to_process():
    return (User.query.filter(User.photo_path.isnot(None))
            .filter((User.face_model.is_(None)) | (User.face_model != MODEL_ID)).all())


# -------------------------------------------------------------- check-ins
def verify_checkin(c: Checkin):
    """Fill c.verify_status / verify_score (caller commits)."""
    if not c.snapshot_path:
        status, score = "no_snapshot", None
    elif not (c.user and c.user.face_embedding and c.user.face_model == MODEL_ID):
        status, score = "no_reference", None
    else:
        path = current_app.config["SNAPSHOTS_DIR"] / c.snapshot_path.split("/")[-1]
        try:
            score = engine().similarity(engine().embed(path), c.user.face_embedding)
            status = "match" if score >= current_app.config["FACE_MIN_SIMILARITY"] else "mismatch"
        except NoFace:
            status, score = "no_face", None
        except OSError:  # snapshot file missing
            status, score = "no_snapshot", None
    c.verify_status, c.verify_score, c.verified_at = status, (round(score, 4) if score is not None else None), utcnow()
    if c.flagged:
        log.warning("Check-in %s of %s flagged by the face check: %s%s", c.uuid, c.user_id, status,
                    f" (similarity {score:.2f})" if score is not None else "")


def checkins_to_process(limit=BATCH):
    """Not checked yet, or waiting for a user photo that is now available."""
    with_ref = db.session.query(User.id).filter(User.face_embedding.isnot(None), User.face_model == MODEL_ID)
    return (Checkin.query.filter((Checkin.verify_status.is_(None))
                                 | ((Checkin.verify_status == "no_reference") & Checkin.user_id.in_(with_ref)))
            .order_by(Checkin.id).limit(limit).all())


def run_once():
    """Process pending users and check-ins. Returns (users, checkins) processed."""
    if not engine().ok:
        return 0, 0
    n_users = 0
    for u in users_to_process():
        try:
            compute_user_face(u)
        except NoFace as exc:
            log.warning("User %s: no usable face in the photo (%s)", u.id, exc)
        except OSError as exc:
            log.warning("User %s: photo unreadable (%s)", u.id, exc)
            u.face_model = MODEL_ID
        db.session.commit()
        n_users += 1
    n_checkins = 0
    while True:
        batch = checkins_to_process()
        if not batch:
            break
        for c in batch:
            verify_checkin(c)
        db.session.commit()
        n_checkins += len(batch)
    if n_users or n_checkins:
        log.info("Face check: %d user photo(s), %d check-in(s) processed", n_users, n_checkins)
    return n_users, n_checkins


def _locked_run(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            return run_once()
        with db.engine.connect() as conn:
            if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar():
                return 0, 0  # another gunicorn worker is on it
            try:
                return run_once()
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                conn.commit()


def start_worker(app):
    """Background pass every minute: new photos, check-ins received while the models were loading."""
    def loop():
        app.extensions["face_engine"].wait_ready()
        while True:
            try:
                _locked_run(app)
            except Exception:
                log.exception("Face check pass failed")
                with app.app_context():
                    db.session.rollback()
            time.sleep(WORKER_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="face-check").start()

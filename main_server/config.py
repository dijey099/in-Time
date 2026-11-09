import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Required: the server refuses to start with an empty, default or short value (see app.check_config)
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://in-time:in-time@localhost:5432/in-time"
    )
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Admin session cookie. Set SESSION_COOKIE_SECURE=true when the console is served over HTTPS.
    SESSION_COOKIE_NAME = "intime_admin"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE")

    # User photos are stored as <PHOTOS_DIR>/<user id>.jpg|png
    PHOTOS_DIR = (BASE_DIR / os.environ.get("PHOTOS_DIR", "photos")).resolve()
    # Snapshots taken by phones at check-in time
    SNAPSHOTS_DIR = (BASE_DIR / os.environ.get("SNAPSHOTS_DIR", "snapshots")).resolve()

    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    # Required: at least 12 characters, not a default value
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
    # Admin login: after this many failed logins in a row (from any address) the login is locked
    # until an administrator removes ADMIN_LOCK_FILE on the server (see README, "Admin login lock").
    LOGIN_MAX_FAILURES = max(1, int(os.environ.get("LOGIN_MAX_FAILURES", "5")))
    ADMIN_LOCK_FILE = Path(os.environ.get("ADMIN_LOCK_FILE", "admin_login.lock"))
    if not ADMIN_LOCK_FILE.is_absolute():
        ADMIN_LOCK_FILE = BASE_DIR / ADMIN_LOCK_FILE

    # Set to the number of reverse proxies in front of the app (nginx = 1) so the
    # relay IP check uses the real client address from X-Forwarded-For.
    TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))
    # Addresses allowed to act as that proxy (send X-Forwarded-For / CF-Connecting-IP).
    # Forwarded headers from any other address are ignored. Default: this machine only.
    # Add the proxy's address if it runs elsewhere (e.g. a Docker network 172.17.0.0/16).
    TRUSTED_PEERS = [p.strip() for p in os.environ.get("TRUSTED_PEERS", "127.0.0.1/32,::1/128").split(",")
                     if p.strip()]

    # Behind Cloudflare: take the relay IP from the CF-Connecting-IP header.
    # The header is only trusted when the request comes from Cloudflare (its IP
    # ranges) or from a TRUSTED_PEERS address (nginx on this host, cloudflared tunnel).
    CF_PROXY = _bool("CF_PROXY")
    # Cloudflare IP ranges (one CIDR per line), kept current by update_cf_ips.py.
    # Re-read automatically when the file changes.
    CF_IPS_FILE = Path(os.environ.get("CF_IPS_FILE", "cf_ips.txt"))
    if not CF_IPS_FILE.is_absolute():
        CF_IPS_FILE = BASE_DIR / CF_IPS_FILE

    MAX_CONTENT_LENGTH = 32 * 1024 * 1024
    # Largest snapshot accepted from a relay (bytes)
    MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

    # Server-side face recognition (OpenCV): checks user photos and re-checks every check-in
    # snapshot against the user's photo. Models (~39 MB) are downloaded here on first start.
    FACE_MODELS_DIR = (BASE_DIR / os.environ.get("FACE_MODELS_DIR", "models")).resolve()
    # Similarity (0-1) between snapshot and photo required for a "match" (higher = stricter)
    FACE_MIN_SIMILARITY = float(os.environ.get("FACE_MIN_SIMILARITY", "0.363"))
    # A snapshot this similar to the reference photo is the photo itself (or a copy): flagged "replay"
    FACE_REPLAY_SIMILARITY = float(os.environ.get("FACE_REPLAY_SIMILARITY", "0.97"))

    # Check-in time checks: a timestamp more than CHECKIN_MAX_FUTURE_SECONDS ahead of the server
    # clock is refused; one received more than CHECKIN_MAX_DELAY_SECONDS after it was made, while
    # the relay was connected, is flagged "delayed".
    CHECKIN_MAX_FUTURE_SECONDS = int(os.environ.get("CHECKIN_MAX_FUTURE_SECONDS", "300"))
    CHECKIN_MAX_DELAY_SECONDS = int(os.environ.get("CHECKIN_MAX_DELAY_SECONDS", "600"))

    # Link to the source code shown in the admin console (AGPL-3.0, section 13). If you run a modified
    # version, point it to your own published source.
    SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/dijey099/in-Time").strip()

    # Departments a user can belong to (comma separated in .env), e.g. Sales,Direction,Manager
    DEPARTMENTS = list(dict.fromkeys(d.strip() for d in os.environ.get("DEPARTMENTS", "").split(",") if d.strip()))

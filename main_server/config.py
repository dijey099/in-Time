import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://in-time:in-time@localhost:5432/in-time"
    )
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # User photos are stored as <PHOTOS_DIR>/<user id>.jpg|png
    PHOTOS_DIR = (BASE_DIR / os.environ.get("PHOTOS_DIR", "photos")).resolve()
    # Snapshots taken by phones at check-in time
    SNAPSHOTS_DIR = (BASE_DIR / os.environ.get("SNAPSHOTS_DIR", "snapshots")).resolve()

    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

    # Set to the number of reverse proxies in front of the app (nginx = 1) so the
    # relay IP check uses the real client address from X-Forwarded-For.
    TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))

    # Behind Cloudflare: take the relay IP from the CF-Connecting-IP header.
    # The header is only trusted when the request comes from Cloudflare (its IP
    # ranges) or from a local/private address (nginx on the same host, cloudflared tunnel).
    CF_PROXY = os.environ.get("CF_PROXY", "false").strip().lower() in ("1", "true", "yes", "on")
    # Cloudflare IP ranges (one CIDR per line), kept current by update_cf_ips.py.
    # Re-read automatically when the file changes.
    CF_IPS_FILE = Path(os.environ.get("CF_IPS_FILE", "cf_ips.txt"))
    if not CF_IPS_FILE.is_absolute():
        CF_IPS_FILE = BASE_DIR / CF_IPS_FILE

    MAX_CONTENT_LENGTH = 32 * 1024 * 1024

    # Server-side face recognition (OpenCV): checks user photos and re-checks every check-in
    # snapshot against the user's photo. Models (~39 MB) are downloaded here on first start.
    FACE_MODELS_DIR = (BASE_DIR / os.environ.get("FACE_MODELS_DIR", "models")).resolve()
    # Similarity (0-1) between snapshot and photo required for a "match" (higher = stricter)
    FACE_MIN_SIMILARITY = float(os.environ.get("FACE_MIN_SIMILARITY", "0.363"))

    # Departments a user can belong to (comma separated in .env), e.g. Sales,Direction,Manager
    DEPARTMENTS = list(dict.fromkeys(d.strip() for d in os.environ.get("DEPARTMENTS", "").split(",") if d.strip()))

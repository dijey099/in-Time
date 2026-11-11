"""Configuration of the main server: main.conf (INI file next to this one, see main.conf.example).

main.conf is required: without it the server does not start (app.check_config). It is the only place
settings come from. IN_TIME_CONF can point to a main.conf elsewhere (e.g. for tests).
"""
import configparser
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONF_FILE = Path(os.environ.get("IN_TIME_CONF") or BASE_DIR / "main.conf")

# setting -> (section, option in main.conf)
KEYS = {
    "secret_key": ("security", "secret_key"),
    "cookie_secure": ("security", "session_cookie_secure"),
    "database_url": ("database", "url"),
    "admin_username": ("admin", "username"),
    "admin_password": ("admin", "password"),
    "max_failures": ("admin", "max_failures"),
    "lock_minutes": ("admin", "lock_minutes"),
    "bind": ("server", "bind"),
    "workers": ("server", "workers"),
    "trusted_proxies": ("server", "trusted_proxies"),
    "trusted_peers": ("server", "trusted_peers"),
    "cloudflare": ("server", "cloudflare"),
    "cf_ips_file": ("server", "cloudflare_ips_file"),
    "photos_dir": ("storage", "photos_dir"),
    "snapshots_dir": ("storage", "snapshots_dir"),
    "face_models_dir": ("storage", "face_models_dir"),
    "face_min": ("face", "min_similarity"),
    "face_replay": ("face", "replay_similarity"),
    "max_future": ("checkins", "max_future_seconds"),
    "max_delay": ("checkins", "max_delay_seconds"),
    "tile_url": ("map", "tile_url"),
    "tile_attribution": ("map", "tile_attribution"),
    "search_url": ("map", "search_url"),
    "source_url": ("about", "source_url"),
}


def load(conf_file=None):
    """Settings from main.conf: {key: str or None (line missing)}. Empty if the file doesn't exist."""
    conf_file = conf_file or CONF_FILE
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None, strict=False)
    if conf_file.exists():
        parser.read(conf_file, encoding="utf-8")
    return {key: parser.get(section, option).strip() if parser.has_option(section, option) else None
            for key, (section, option) in KEYS.items()}


CONF_FOUND = CONF_FILE.is_file()
SETTINGS = load()


def _get(key, default):
    value = SETTINGS.get(key)
    return default if value is None else value


def _bool(key, default="false"):
    return (_get(key, default) or default).strip().lower() in ("1", "true", "yes", "on")


def _num(key, default, cast=int):
    value = _get(key, "")
    try:
        return cast(value) if value != "" else cast(default)
    except ValueError:
        raise ValueError(f"main.conf: {KEYS[key][1]} in [{KEYS[key][0]}] must be a number (got {value!r})") from None


class Config:
    CONF_FILE = CONF_FILE
    CONF_FOUND = CONF_FOUND
    # Required: the server refuses to start with an empty, default or short value (see app.check_config)
    SECRET_KEY = _get("secret_key", "")
    SQLALCHEMY_DATABASE_URI = _get("database_url", "") or "postgresql+psycopg://in-time:in-time@localhost:5432/in-time"
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Admin session cookie. Set session_cookie_secure = true ([security]) when the console is served over HTTPS.
    SESSION_COOKIE_NAME = "intime_admin"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("cookie_secure")

    # Collaborator photos are stored as <PHOTOS_DIR>/<collaborator id>.jpg|png
    PHOTOS_DIR = (BASE_DIR / (_get("photos_dir", "") or "photos")).resolve()
    # Snapshots taken by phones at check-in time
    SNAPSHOTS_DIR = (BASE_DIR / (_get("snapshots_dir", "") or "snapshots")).resolve()

    ADMIN_USERNAME = _get("admin_username", "") or "itadmin"
    # Required: at least 12 characters, not a default value
    ADMIN_PASSWORD = _get("admin_password", "")
    # Console login lock, per account: LOGIN_MAX_FAILURES wrong passwords for the same account within
    # LOGIN_LOCK_MINUTES lock it until an Administrator unlocks it (Settings > Users). The built-in
    # administrator of main.conf is never locked: it is paused for LOGIN_LOCK_MINUTES instead.
    LOGIN_MAX_FAILURES = max(1, _num("max_failures", 5))
    LOGIN_LOCK_MINUTES = max(1, _num("lock_minutes", 15))

    # Set to the number of reverse proxies in front of the app (nginx = 1) so the
    # relay IP check uses the real client address from X-Forwarded-For.
    TRUSTED_PROXIES = _num("trusted_proxies", 0)
    # Addresses allowed to act as that proxy (send X-Forwarded-For / CF-Connecting-IP).
    # Forwarded headers from any other address are ignored. Default: this machine only.
    # Add the proxy's address if it runs elsewhere (e.g. a Docker network 172.17.0.0/16).
    TRUSTED_PEERS = [p.strip() for p in (_get("trusted_peers", "") or "127.0.0.1/32,::1/128").split(",")
                     if p.strip()]

    # Behind Cloudflare: take the relay IP from the CF-Connecting-IP header.
    # The header is only trusted when the request comes from Cloudflare (its IP
    # ranges) or from a TRUSTED_PEERS address (nginx on this host, cloudflared tunnel).
    CF_PROXY = _bool("cloudflare")
    # Cloudflare IP ranges (one CIDR per line), kept current by update_cf_ips.py.
    # Re-read automatically when the file changes.
    CF_IPS_FILE = Path(_get("cf_ips_file", "") or "cf_ips.txt")
    if not CF_IPS_FILE.is_absolute():
        CF_IPS_FILE = BASE_DIR / CF_IPS_FILE

    MAX_CONTENT_LENGTH = 32 * 1024 * 1024
    # Largest snapshot accepted from a relay (bytes)
    MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

    # Server-side face recognition (OpenCV): checks collaborator photos and re-checks every check-in
    # snapshot against the collaborator's photo. Models (~39 MB) are downloaded here on first start.
    FACE_MODELS_DIR = (BASE_DIR / (_get("face_models_dir", "") or "models")).resolve()
    # Similarity (0-1) between snapshot and photo required for a "match" (higher = stricter)
    FACE_MIN_SIMILARITY = _num("face_min", 0.363, float)
    # A snapshot this similar to the reference photo is the photo itself (or a copy): flagged "replay"
    FACE_REPLAY_SIMILARITY = _num("face_replay", 0.97, float)

    # Check-in time checks: a timestamp more than CHECKIN_MAX_FUTURE_SECONDS ahead of the server
    # clock is refused; one received more than CHECKIN_MAX_DELAY_SECONDS after it was made, while
    # the relay was connected, is flagged "delayed".
    CHECKIN_MAX_FUTURE_SECONDS = _num("max_future", 300)
    CHECKIN_MAX_DELAY_SECONDS = _num("max_delay", 600)

    # Link to the source code shown in the admin console (AGPL-3.0, section 13). If you run a modified
    # version, point it to your own published source.
    SOURCE_URL = (_get("source_url", "https://github.com/dijey099/in-Time") or "").strip()
    # Map of the relay site area picker (admin console). Default: OpenStreetMap's volunteer-run tile
    # servers, fine for a few admins (https://operations.osmfoundation.org/policies/tiles/). For more
    # use, point it to another provider (e.g. https://{s}.tile.example.com/{z}/{x}/{y}.png?key=...).
    MAP_TILE_URL = (_get("tile_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png") or "").strip()
    MAP_TILE_ATTRIBUTION = (_get(
        "tile_attribution",
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors') or "").strip()
    # Address search on the map (Nominatim API). Empty: no search box, the site is clicked on the map.
    MAP_SEARCH_URL = (_get("search_url", "https://nominatim.openstreetmap.org/search") or "").strip()

    # Gunicorn (systemd service, gunicorn.conf.py)
    SERVER_BIND = _get("bind", "") or "127.0.0.1:8000"
    SERVER_WORKERS = max(1, _num("workers", 2))

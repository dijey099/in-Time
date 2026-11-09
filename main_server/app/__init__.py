import ipaddress
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from .models import db


class ConfigError(RuntimeError):
    """Invalid or missing configuration: the server refuses to start."""


WEAK_VALUES = {"", "admin", "change-me", "password", "test-key", "replace-with-a-long-random-string",
               "replace-with-a-strong-password"}


def check_config(cfg):
    departments = cfg.get("DEPARTMENTS") or []
    if not departments:
        raise ConfigError(
            "DEPARTMENTS is not set in main_server/.env. Every user must belong to a department, "
            "so the main server can't start without it. Example: DEPARTMENTS=Sales,Direction,Manager")
    too_long = [d for d in departments if len(d) > 100]
    if too_long:
        raise ConfigError(f"DEPARTMENTS: names must be at most 100 characters ({too_long[0][:40]}...)")
    if cfg.get("TESTING"):
        return
    secret = cfg.get("SECRET_KEY") or ""
    if secret in WEAK_VALUES or len(secret) < 32:
        raise ConfigError("SECRET_KEY in main_server/.env is missing, a default value or shorter than 32 "
                          "characters. Generate one with: python3 -c \"import secrets; "
                          "print(secrets.token_urlsafe(48))\"")
    password = cfg.get("ADMIN_PASSWORD") or ""
    if password in WEAK_VALUES or len(password) < 12 or password == cfg.get("ADMIN_USERNAME"):
        raise ConfigError("ADMIN_PASSWORD in main_server/.env is missing, a default value or shorter than 12 "
                          "characters: set a strong password.")


def _networks(values):
    nets = []
    for v in values:
        try:
            nets.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            raise ConfigError(f"TRUSTED_PEERS: invalid address or range {v!r}")
    return nets


def peer_trusted(addr, nets) -> bool:
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    return any(ip in n for n in nets)


class TrustedProxyFix:
    """ProxyFix applied only to requests that come from a trusted proxy (TRUSTED_PEERS).
    Anyone else reaching the app port directly can't set their IP with X-Forwarded-For."""

    def __init__(self, app, nets, n):
        self.app, self.nets = app, nets
        self.fixed = ProxyFix(app, x_for=n, x_proto=n, x_host=n)

    def __call__(self, environ, start_response):
        if peer_trusted(environ.get("REMOTE_ADDR"), self.nets):
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    check_config(app.config)  # before touching the database
    app.config["TRUSTED_PEER_NETS"] = _networks(app.config.get("TRUSTED_PEERS") or [])

    if app.config["TRUSTED_PROXIES"]:
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, app.config["TRUSTED_PEER_NETS"], app.config["TRUSTED_PROXIES"])

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return resp

    app.config["PHOTOS_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["SNAPSHOTS_DIR"].mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    from .api import api_bp
    from .admin import admin_bp
    from .cli import register_cli

    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(admin_bp)
    register_cli(app)

    with app.app_context():
        db.create_all()
        from .migrate import run as migrate
        migrate(db)

    # face-api.js models used by the admin pages (downloaded once if missing)
    from .face_models import ModelStatus
    app.extensions["face_models"] = models = ModelStatus(Path(app.static_folder) / "vendor" / "face-api")
    models.start_download()

    # Server-side face check (OpenCV): user photos and check-in snapshots
    from .face_engine import FaceEngine
    from .verify import start_worker
    app.extensions["face_engine"] = FaceEngine(app.config["FACE_MODELS_DIR"],
                                               app.config["FACE_MIN_SIMILARITY"]).start()
    start_worker(app)

    return app

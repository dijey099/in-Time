from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from .models import db


class ConfigError(RuntimeError):
    """Invalid or missing configuration: the server refuses to start."""


def check_config(cfg):
    departments = cfg.get("DEPARTMENTS") or []
    if not departments:
        raise ConfigError(
            "DEPARTMENTS is not set in main_server/.env. Every user must belong to a department, "
            "so the main server can't start without it. Example: DEPARTMENTS=Sales,Direction,Manager")
    too_long = [d for d in departments if len(d) > 100]
    if too_long:
        raise ConfigError(f"DEPARTMENTS: names must be at most 100 characters ({too_long[0][:40]}...)")


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    check_config(app.config)  # before touching the database

    if app.config["TRUSTED_PROXIES"]:
        n = app.config["TRUSTED_PROXIES"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    if app.config["SECRET_KEY"] in ("change-me", "replace-with-a-long-random-string"):
        app.logger.warning("SECRET_KEY is the default value: set a long random SECRET_KEY in .env")
    if app.config["ADMIN_PASSWORD"] in ("admin", "change-me"):
        app.logger.warning("ADMIN_PASSWORD is a default value: set a strong ADMIN_PASSWORD in .env")

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

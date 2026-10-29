import sys

from app import ConfigError, create_app

try:
    app = create_app()
except ConfigError as exc:
    if __name__ == "__main__":
        sys.exit(f"Main server not started: {exc}")
    raise  # gunicorn / flask CLI: show the error and stop

if __name__ == "__main__":
    # Development only. In production: gunicorn -w 4 -b 0.0.0.0:8000 main:app (behind nginx + HTTPS)
    app.run(host="0.0.0.0", port=8000, debug=True)

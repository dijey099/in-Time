import os
import sys

from app import ConfigError, create_app

try:
    app = create_app()
except ConfigError as exc:
    if __name__ == "__main__":
        sys.exit(f"Main server not started: {exc}")
    raise  # gunicorn / flask CLI: show the error and stop

if __name__ == "__main__":
    # Development only, on this machine. In production: gunicorn -b 127.0.0.1:8000 main:app (behind nginx + HTTPS).
    # FLASK_DEBUG=1 turns on the debugger: never on a machine others can reach.
    app.run(host="0.0.0.0", port=8000, debug=os.environ.get("FLASK_DEBUG") == "1")

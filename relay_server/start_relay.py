"""in-Time relay server: Flask (private HTTPS API for phones) + Qt GUI + SQLite cache.

    python start_relay.py            # with GUI
    python start_relay.py --no-gui   # headless (service / testing)

Double-clicked, the Windows executable hides its console: the log is in data/relay.log and
start-up errors are shown in a message box. Run from a terminal, it prints the log there.
"""
import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from relay.certs import ensure_certificate, lan_ips
from relay.clock import TrustedClock
from relay.config import load_config
from relay.db import Database
from relay.face_engine import FaceEngine
from relay.face_models import ModelStatus
from relay.identity import Identifier
from relay.sync import SyncWorker
from relay.web_api import WebServer, create_web_app


def show_error(message: str):
    """Start-up error in a message box: the Windows executable has no console."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 (needed by the box)
        QMessageBox.critical(None, "in-Time relay", message)
    except Exception:  # no display (e.g. service): the message is in stderr / relay.log
        pass


def main():
    parser = argparse.ArgumentParser(description="in-Time relay server")
    parser.add_argument("--config", type=Path, help="path to relay.ini")
    parser.add_argument("--no-gui", action="store_true", help="run without the Qt window")
    args = parser.parse_args()
    try:
        run(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):  # configuration error with a message
            if logging.getLogger().handlers:  # relay.log is open
                logging.getLogger("relay").error(exc.code)
            if not args.no_gui:
                if sys.stderr:
                    print(exc.code, file=sys.stderr)
                show_error(exc.code)
                raise SystemExit(1)
        raise
    except Exception as exc:
        logging.getLogger("relay").exception("Relay stopped")
        if args.no_gui:
            raise
        show_error(f"The relay could not start:\n\n{type(exc).__name__}: {exc}")
        raise SystemExit(1)


def run(args):
    cfg = load_config(args.config)
    handlers = [logging.FileHandler(cfg.data_dir / "relay.log", encoding="utf-8")]
    if sys.stderr:  # None in the windowed executable (no console)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    log = logging.getLogger("relay")

    # Trusted time (Google over HTTPS) + internet check: no internet = no check-in
    clock = TrustedClock(cfg.time_urls, cfg.tz, check_interval=cfg.time_check_interval)
    clock.sync_once()  # first check before accepting phones
    clock.start()
    if cfg.tz_from_pc:
        log.warning("No [time] timezone in relay.ini: using the PC time zone (%s). Set it to prevent tampering.",
                    cfg.tz_name)

    db = Database(cfg.db_path, cfg.tz, cfg.photos_dir)
    # Face recognition runs here, on the relay (OpenCV); phones only take the picture
    engine = FaceEngine(cfg.face_models_dir, cfg.min_similarity).start()
    identifier = Identifier(engine, db, cfg.min_similarity)
    identifier.start()
    sync = SyncWorker(cfg, db, clock, on_users_synced=identifier.refresh)
    cert, key = ensure_certificate(cfg.certs_dir)
    models = ModelStatus(cfg.vendor_dir)  # face detector used by the phone page (face-api.js)
    models.start_download()  # no-op when everything is present
    web = WebServer(create_web_app(cfg, db, clock, identifier, on_new_checkin=sync.wake, sync_state=sync.state,
                                   model_status=models),
                    cfg.host, cfg.port, cert, key)

    phone_urls = [f"https://{ip}:{cfg.port}" for ip in lan_ips() if not ip.startswith("127.")] \
        or [f"https://127.0.0.1:{cfg.port}"]
    log.info("Phones can connect to: %s", ", ".join(phone_urls))

    sync.start()
    web.start()

    if args.no_gui:
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        stop.wait()
        code = 0
    else:
        import shiboken6
        from PySide6.QtWidgets import QApplication
        from relay.gui import MainWindow

        if sys.platform == "win32":  # own taskbar icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("in-time.relay")
        from PySide6.QtGui import QColor, QIcon, QPalette
        from relay.gui import BRAND, LOGO_PATH

        app = QApplication(sys.argv)
        app.setApplicationName("in-Time relay")
        if LOGO_PATH.exists():
            app.setWindowIcon(QIcon(str(LOGO_PATH)))
        pal = app.palette()  # keep the system theme, use the brand blue for selections
        pal.setColor(QPalette.Highlight, QColor(BRAND))
        pal.setColor(QPalette.HighlightedText, QColor("white"))
        app.setPalette(pal)
        win = MainWindow(cfg, db, sync, phone_urls, [models, identifier], clock)
        win.show()
        code = app.exec()
        # Destroy the window while the QApplication still exists. Otherwise Python frees both
        # at exit in an arbitrary order (the application first), and the process aborts with
        # a core dump when the window is closed.
        shiboken6.delete(win)
        del win

    sync.stop()
    clock.stop()
    web.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()

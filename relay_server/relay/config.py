import configparser
import sys
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def bundle_dir() -> Path:
    """Read-only program files (web page, logo). Inside a PyInstaller build they are
    unpacked to a temporary folder (sys._MEIPASS), so they must be bundled with --add-data."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def app_dir() -> Path:
    """Folder containing relay.ini and data/ (works for PyInstaller builds too)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    main_url: str
    token: str
    verify_ssl: object  # True/False or path to a CA bundle
    sync_interval: int
    host: str
    port: int
    min_similarity: float  # face match: cosine similarity required (higher = stricter)
    duplicate_window: int
    allow_public_clients: bool
    data_dir: Path
    web_dir: Path
    vendor_dir: Path  # face-api.js + models (downloaded at first start)
    time_urls: list
    time_check_interval: int
    tz: tzinfo
    tz_name: str
    tz_from_pc: bool  # True when [time] timezone is not set (PC setting used)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "relay.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def face_models_dir(self) -> Path:  # face recognition models (downloaded once)
        return self.data_dir / "models"

    @property
    def certs_dir(self) -> Path:
        return self.data_dir / "certs"


def load_config(path: Path | None = None) -> Config:
    base = app_dir()
    path = path or base / "relay.ini"
    cp = configparser.ConfigParser()
    if not path.exists():
        raise SystemExit(f"Configuration file not found: {path}\nCopy relay.ini.example to relay.ini and edit it.")
    cp.read(path, encoding="utf-8")

    verify = cp.get("main_server", "verify_ssl", fallback="true").strip()
    if verify.lower() in ("true", "yes", "1"):
        verify_ssl = True
    elif verify.lower() in ("false", "no", "0"):
        verify_ssl = False
    else:
        verify_ssl = verify  # path to CA bundle

    data_dir = Path(cp.get("relay", "data_dir", fallback=str(base / "data")))
    if not data_dir.is_absolute():
        data_dir = base / data_dir

    tz_name = cp.get("time", "timezone", fallback="").strip()
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"Unknown time zone in relay.ini: {tz_name!r} (example: Indian/Antananarivo)")
    else:
        tz = datetime.now().astimezone().tzinfo  # falls back to the PC setting (fixed offset)
        off = int(datetime.now(tz).utcoffset().total_seconds() // 60)
        tz_name = f"UTC{'+' if off >= 0 else '-'}{abs(off) // 60:02d}:{abs(off) % 60:02d}"
    urls = [u.strip() for u in cp.get("time", "urls", fallback="https://www.google.com/generate_204").split(",")
            if u.strip()]

    cfg = Config(
        main_url=cp.get("main_server", "url").rstrip("/"),
        token=cp.get("main_server", "token", fallback="").strip(),
        verify_ssl=verify_ssl,
        sync_interval=cp.getint("main_server", "sync_interval", fallback=30),
        host=cp.get("relay", "host", fallback="0.0.0.0"),
        port=cp.getint("relay", "port", fallback=8443),
        min_similarity=cp.getfloat("face", "min_similarity", fallback=0.363),
        duplicate_window=cp.getint("relay", "duplicate_window", fallback=60),
        allow_public_clients=cp.getboolean("relay", "allow_public_clients", fallback=False),
        data_dir=data_dir,
        web_dir=bundle_dir() / "relay" / "web",
        # An executable unpacks to a new temp folder on every start: keep the downloaded
        # models next to it (data/vendor) so they are only downloaded once.
        vendor_dir=(data_dir / "vendor" if getattr(sys, "frozen", False)
                    else bundle_dir() / "relay" / "web" / "vendor") / "face-api",
        time_urls=urls,
        time_check_interval=cp.getint("time", "check_interval", fallback=60),
        tz=tz,
        tz_name=tz_name,
        tz_from_pc=not cp.get("time", "timezone", fallback="").strip(),
    )
    for d in (cfg.data_dir, cfg.photos_dir, cfg.snapshots_dir, cfg.certs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg

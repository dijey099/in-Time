"""Stable per-machine ID for this relay server.

The ID is derived from the operating system's own machine GUID, so it is the same every time on
the same computer. It is also written to data/server.id as a record; if that file is deleted it is
recreated with the same value, because the value comes from the OS, not from the file. A different
computer (or a fresh OS install) gets a different ID, so a relay token can't be shared between computers.
"""
import hashlib
import logging
import subprocess
import sys
import uuid
from pathlib import Path

log = logging.getLogger("relay.machine")

# Mixed into the hash so the ID can't be matched to the raw OS GUID and is specific to in-Time.
_SALT = b"in-Time/relay/v1"


def _os_guid() -> str | None:
    """A string unique to this machine, from the OS. None if it can't be read."""
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                 winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            try:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(value).strip() or None
            finally:
                winreg.CloseKey(key)
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2].strip() or None
            return None
        # Linux / BSD
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            p = Path(path)
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    return text
        return None
    except Exception as exc:  # never fail because of this
        log.debug("Could not read the OS machine GUID: %s", exc)
        return None


def _derive(guid: str) -> str:
    return hashlib.sha256(_SALT + guid.encode("utf-8")).hexdigest()[:32]


def get_machine_id(data_dir: Path) -> str:
    """Return this relay's stable ID, writing data/server.id as a record."""
    f = Path(data_dir) / "server.id"
    guid = _os_guid()
    if guid:
        mid = _derive(guid)  # deterministic: survives deleting server.id
    elif f.exists():
        mid = f.read_text(encoding="utf-8").strip() or uuid.uuid4().hex
    else:
        # No OS GUID available: fall back to a random ID, kept in server.id
        mid = uuid.uuid4().hex
        log.warning("No OS machine GUID available: using a stored random server ID")
    try:
        if not f.exists() or f.read_text(encoding="utf-8").strip() != mid:
            f.write_text(mid, encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s: %s", f, exc)
    return mid

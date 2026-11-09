"""Secrets of the relay (main server token, identity and data keys) kept out of relay.ini.

On Windows they are encrypted with DPAPI for the Windows account that runs the relay: copying the
data folder to another PC or reading it from another account gives nothing usable. Elsewhere they
are stored in a file only the relay's user can read.

On every system the file is also encrypted (AES-256-GCM) with a key derived from a secret built
into the relay program and this machine's OS GUID: the file is useless on another computer and
can't be read or changed with a simple script, even from the relay's own account.

The store holds the relay's identity key and the data key that encrypts its cache (see crypto.py):
if secrets.bin is deleted, check-ins not yet sent to the main server can't be read any more.

A value typed in relay.ini is taken as the new value: it is moved here and removed from relay.ini.
Data folder permissions are also restricted to the relay's account (photos and snapshots are
biometric data).
"""
import base64
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("relay.secrets")

_ENTROPY = b"in-Time/relay/secrets/v1"
# Built into the program (obfuscated build): second layer of protection of secrets.bin
_APP_SECRET = base64.b64decode("3x+S6kR9y+jDsQlHgW3n5RoACUA8/2lnzyXGJL0kWP4=")
_MAGIC = b"ITS2"


# ------------------------------------------------------------------ Windows DPAPI
if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _protect(data: bytes) -> bytes:
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        src, _keep = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if not crypt32.CryptProtectData(ctypes.byref(src), "in-Time relay", ctypes.byref(ent), None, None,
                                        0x1, ctypes.byref(out)):  # CRYPTPROTECT_UI_FORBIDDEN
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        src, _keep = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                          0x1, ctypes.byref(out)):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
else:
    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data


def _wrap_key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from .machine_id import _os_guid
    salt = (_os_guid() or "no-os-guid").encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"in-Time relay secrets v2").derive(_APP_SECRET)


def _seal(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return _MAGIC + nonce + AESGCM(_wrap_key()).encrypt(nonce, data, _MAGIC)


def _unseal(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(_wrap_key()).decrypt(blob[4:16], blob[16:], _MAGIC)


class SecretStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "secrets.bin"
        self._values = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_bytes()
            if raw.startswith(_MAGIC):
                return json.loads(_unprotect(_unseal(raw)).decode("utf-8"))
            values = json.loads(_unprotect(base64.b64decode(raw)).decode("utf-8"))  # older format
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"Can't read the relay's stored secrets ({self.path}): {type(exc).__name__} {exc}. "
                             f"They can only be read on this computer by the account that saved them. Run the "
                             f"relay with that account. Otherwise delete {self.path.name}, generate a new token for "
                             f"this relay on the main server (Relays page) and type it in relay.ini: check-ins not "
                             f"yet sent to the main server will be lost.")
        self._values = values
        self._save()  # upgrade to the current format
        return values

    def _save(self):
        raw = _seal(_protect(json.dumps(self._values).encode("utf-8")))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get(self, name, default=""):
        return self._values.get(name, default)

    def set(self, name, value):
        if self._values.get(name) != value:
            self._values[name] = value
            self._save()

    def delete(self, name):
        if name in self._values:
            del self._values[name]
            self._save()


def take_from_ini(ini_path: Path, store: SecretStore, entries):
    """entries: [(section, option, secret name)]. A value typed in relay.ini replaces the stored
    one, then is blanked in relay.ini (comments and layout are kept)."""
    try:
        lines = ini_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    section, changed = None, False
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        for sec, opt, name in entries:
            m = re.match(rf"(\s*{re.escape(opt)}\s*[=:]\s*)(\S.*?)\s*$", line, re.IGNORECASE)
            if section == sec and m:
                store.set(name, m.group(2).strip())
                end = "\r\n" if line.endswith("\r\n") else "\n"
                lines[i] = m.group(1).rstrip() + " " + end
                changed = True
                log.info("[%s] %s moved from relay.ini to the protected store", sec, opt)
    if changed:
        try:
            ini_path.write_text("".join(lines), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not remove the secrets from %s: %s", ini_path, exc)


def restrict_dir(path: Path):
    """Only the relay's account (plus SYSTEM and Administrators on Windows) can open the data folder."""
    try:
        if os.name == "posix":
            os.chmod(path, 0o700)
            return
        if sys.platform == "win32":
            user = os.environ.get("USERNAME", "")
            domain = os.environ.get("USERDOMAIN", "")
            if not user:
                return
            account = f"{domain}\\{user}" if domain else user
            # *S-1-5-18 = SYSTEM, *S-1-5-32-544 = Administrators (SIDs work in every Windows language)
            subprocess.run(["icacls", str(path), "/inheritance:r",
                            "/grant:r", f"{account}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                           capture_output=True, timeout=30, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001 - never fail the start because of this
        log.warning("Could not restrict the permissions of %s: %s", path, exc)

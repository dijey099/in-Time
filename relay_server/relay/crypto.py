"""Keys of the relay and what they protect.

- Identity key (Ed25519): created on first start, kept in the protected secret store. Its public
  key is sent to the main server, which records it with the relay's first request (bound to the
  relay token). The relay signs every request to the main server and every check-in with it, so a copied
  machine ID, a stolen relay token or an edited database is useless without this key.
- Data key (AES-256-GCM): encrypts the local cache (collaborators, check-ins, settings in relay.db) and the
  photo / snapshot files. Each sealed value is bound to where it is stored (row, file name), so
  values can't be swapped between rows either. Anything changed outside the relay fails to decrypt
  and is ignored.
"""
import base64
import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Fields of a signed check-in: must match main_server/app/api.py (CHECKIN_FIELDS)
CHECKIN_FIELDS = ("uuid", "seq", "relay_id", "collaborator_id", "direction", "ts", "status", "reason", "distance",
                  "timezone", "snapshot_sha256")


class Tampered(Exception):
    """A sealed value that was not written by this relay (changed, moved or from another key)."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ------------------------------------------------------------------ data key
class Vault:
    VERSION = b"\x01"

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("data key must be 32 bytes")
        self._aead = AESGCM(key)
        self._index_key = hmac.new(key, b"in-Time relay index v1", hashlib.sha256).digest()

    def seal(self, data: bytes, where: str) -> bytes:
        nonce = os.urandom(12)
        return self.VERSION + nonce + self._aead.encrypt(nonce, data, where.encode())

    def open(self, blob: bytes, where: str) -> bytes:
        if not blob or blob[:1] != self.VERSION or len(blob) < 29:
            raise Tampered(where)
        try:
            return self._aead.decrypt(blob[1:13], blob[13:], where.encode())
        except InvalidTag:
            raise Tampered(where) from None

    def seal_json(self, value, where: str) -> bytes:
        return self.seal(json.dumps(value, separators=(",", ":")).encode(), where)

    def open_json(self, blob: bytes, where: str):
        return json.loads(self.open(blob, where))

    def tag(self, text: str) -> str:
        """Stable pseudonym of a value (collaborator ID) for indexes and file names."""
        return hmac.new(self._index_key, str(text).encode(), hashlib.sha256).hexdigest()[:32]


# -------------------------------------------------------------- identity key
class Identity:
    def __init__(self, seed: bytes):
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_key = base64.b64encode(self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def sign(self, message: bytes) -> str:
        return b64u(self._key.sign(message))

    def sign_request(self, method: str, path: str, ts: int, body: bytes) -> str:
        return self.sign(request_message(method, path, str(ts), body))

    def sign_checkin(self, record: dict) -> str:
        return self.sign(checkin_message(record))


def request_message(method: str, path: str, ts: str, body: bytes) -> bytes:
    """Must match main_server/app/auth.py (request_message)."""
    return "\n".join(("in-Time relay request v1", method.upper(), path, ts,
                      hashlib.sha256(body).hexdigest())).encode()


def checkin_message(record: dict) -> bytes:
    """Must match main_server/app/api.py (checkin_message).
    "location" ({lat, lng, accuracy} from the phone's GPS) is signed when present; check-ins made
    before it existed keep their original signature."""
    fields = {k: record.get(k) for k in CHECKIN_FIELDS}
    if record.get("location") is not None:
        fields["location"] = record["location"]
    return b"in-Time checkin v2\n" + json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def load_keys(store):
    """(Vault, Identity) from the secret store, created on first start."""
    data_key = store.get("data_key", "")
    if not data_key:
        data_key = base64.b64encode(os.urandom(32)).decode()
        store.set("data_key", data_key)
    seed = store.get("identity_key", "")
    if not seed:
        seed = base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        store.set("identity_key", seed)
    return Vault(base64.b64decode(data_key)), Identity(base64.b64decode(seed))

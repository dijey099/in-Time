"""Authentication helpers: relay token + IP check, admin session login, CSRF."""
import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from datetime import timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from functools import wraps

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

from .models import LoginFailure, Relay, db, hash_token, utcnow

RELAY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ONLINE_GAP = 300  # seconds without any request from a relay = it was disconnected
MAX_REQUEST_SKEW = 300  # relay request time (its trusted internet time) vs this server's clock


def _unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_ed25519(public_key_b64: str, signature_b64u: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(_unb64u(signature_b64u), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def request_message(method: str, path: str, ts: str, body: bytes) -> bytes:
    """What a relay signs for each request (same function on the relay, relay/crypto.py)."""
    return "\n".join(("in-Time relay request v1", method.upper(), path, ts,
                      hashlib.sha256(body).hexdigest())).encode()


def _api_path():
    """Path from /api/v1/ on, so a reverse proxy that adds or strips a prefix doesn't matter."""
    p = request.path
    i = p.find("/api/v1/")
    return p[i:] if i >= 0 else p


# ------------------------------------------------------------- client address
# Cloudflare ranges come from CF_IPS_FILE (cf_ips.txt), kept current by update_cf_ips.py.
_cf_cache = {"key": None, "nets": []}
_cf_lock = threading.Lock()


def _cf_networks():
    """Networks from cf_ips.txt, re-read whenever the file changes.
    Missing/unreadable file -> empty list (only local/private peers are trusted)."""
    path = current_app.config["CF_IPS_FILE"]
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(path), None, None)
    if key == _cf_cache["key"]:
        return _cf_cache["nets"]
    with _cf_lock:
        if key == _cf_cache["key"]:
            return _cf_cache["nets"]
        nets = []
        if key[1] is None:
            current_app.logger.error("CF_PROXY: %s not found, Cloudflare edges are not trusted. "
                                     "Run update_cf_ips.py", path)
        else:
            for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    nets.append(ipaddress.ip_network(line, strict=False))
                except ValueError:
                    current_app.logger.error("CF_PROXY: ignoring invalid line %d in %s: %r", n, path, line)
            if not nets:
                current_app.logger.error("CF_PROXY: %s holds no IP range. Run update_cf_ips.py", path)
            else:
                current_app.logger.info("CF_PROXY: loaded %d Cloudflare ranges from %s", len(nets), path.name)
        _cf_cache.update(key=key, nets=nets)
        return nets


def _trusted_cf_peer(peer: str) -> bool:
    """The machine that connected to us may relay CF-Connecting-IP: a Cloudflare edge, or a
    TRUSTED_PEERS hop (nginx on this host, cloudflared tunnel). Not any private address: another
    machine on the LAN could otherwise claim any IP."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if any(ip in net for net in current_app.config.get("TRUSTED_PEER_NETS", [])):
        return True
    return any(ip in net for net in _cf_networks())


def client_ip():
    """Real IP of the caller.

    CF_PROXY=true  -> CF-Connecting-IP header (only from a trusted peer), else None
    otherwise      -> request.remote_addr (already corrected by ProxyFix when TRUSTED_PROXIES > 0)
    """
    peer = request.remote_addr or ""
    if not current_app.config.get("CF_PROXY"):
        return peer
    header = request.headers.get("CF-Connecting-IP", "").strip()
    if not header:
        current_app.logger.warning("CF_PROXY is on but CF-Connecting-IP is missing (peer %s)", peer)
        return None
    if not _trusted_cf_peer(peer):
        current_app.logger.warning("Ignored CF-Connecting-IP=%s sent by non-Cloudflare peer %s", header, peer)
        return None
    try:
        return str(ipaddress.ip_address(header))
    except ValueError:
        return None


def peer_ip():
    """Address of the machine that opened the TCP connection, ignoring every proxy header
    (X-Forwarded-For rewritten by ProxyFix, CF-Connecting-IP)."""
    orig = request.environ.get("werkzeug.proxy_fix.orig") or {}
    return orig.get("REMOTE_ADDR") or request.environ.get("REMOTE_ADDR") or ""


# --------------------------------------------------------------------- relays
def ip_allowed(remote_ip: str, allowed: str) -> bool:
    """`allowed` is a comma separated list of IPs or CIDR ranges. '*' = any."""
    entries = [e.strip() for e in (allowed or "").split(",") if e.strip()]
    if "*" in entries:
        return True
    try:
        ip = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _valid_public_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def relay_required(view):
    """Relays authenticate with `Authorization: Bearer <token>` and must call
    from one of the IPs registered for that token. A token belongs to a single relay server:
    the first relay that uses it binds the token to its machine ID (X-Relay-Id header) and to its
    Ed25519 public key (X-Relay-Key header). Another relay using the same token is refused.

    Every request is signed with the relay's private key (headers X-Relay-Time, X-Relay-Signature)
    and checked against the public key recorded at the first request, so a stolen token or a copied
    machine ID is useless without the relay's key. Generating a new token on the Relays page frees
    the binding (replacement relay).

    401 -> missing/invalid token, relay ID, key or signature, 403 -> IP not allowed / relay disabled /
    token used by another relay / relay ID used by another token.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify(error="missing_token"), 401
        token = header[7:].strip()
        relay = Relay.query.filter_by(token_hash=hash_token(token)).first()
        if relay is None:
            return jsonify(error="invalid_token"), 401
        if not relay.enabled:
            return jsonify(error="relay_disabled"), 403
        # One relay per token: bind the token to the first relay's machine ID (X-Relay-Id header).
        relay_id = (request.headers.get("X-Relay-Id") or "").strip()
        if not RELAY_ID_RE.match(relay_id):
            return jsonify(error="missing_relay_id"), 401
        # Local relay: direct connection, the IP is the real peer (proxy settings bypassed)
        remote_ip = peer_ip() if relay.local_relay else (client_ip() or "")
        if not ip_allowed(remote_ip, relay.allowed_ips):
            if not remote_ip:
                current_app.logger.warning("Relay %s refused: real IP unknown (not received through Cloudflare)", relay.name)
                return jsonify(error="ip_not_allowed", ip="unknown (not received through Cloudflare)"), 403
            current_app.logger.warning("Relay %s called from unauthorized IP %s", relay.name, remote_ip)
            return jsonify(error="ip_not_allowed", ip=remote_ip), 403
        if relay.instance_id and relay.instance_id != relay_id:
            current_app.logger.warning("Relay token '%s' used by a second relay server (%s != %s)",
                                       relay.name, relay_id, relay.instance_id)
            return jsonify(error="token_in_use"), 403
        # Public key: the one recorded at the first request, else the one the relay presents now
        public_key = relay.public_key
        if not public_key:
            public_key = (request.headers.get("X-Relay-Key") or "").strip()
            if not _valid_public_key(public_key):
                return jsonify(error="missing_relay_key"), 401
        # Proof that the request comes from the relay holding this private key
        ts = (request.headers.get("X-Relay-Time") or "").strip()
        sig = (request.headers.get("X-Relay-Signature") or "").strip()
        if not ts.isdigit() or not sig:
            return jsonify(error="missing_signature"), 401
        if abs(int(ts) - time.time()) > MAX_REQUEST_SKEW:
            current_app.logger.warning("Relay %s refused: request time %s too far from this server's clock "
                                       "(check the date/time of the main server)", relay.name, ts)
            return jsonify(error="clock_skew"), 401
        if not verify_ed25519(public_key, sig, request_message(request.method, _api_path(), ts, request.get_data())):
            current_app.logger.warning("Relay %s (%s) refused: bad request signature", relay.name, relay_id)
            return jsonify(error="bad_signature"), 401
        if not relay.instance_id:
            other = Relay.query.filter(Relay.instance_id == relay_id, Relay.id != relay.id).first()
            if other:
                current_app.logger.warning("Relay token '%s' refused: relay server %s already uses token '%s'",
                                           relay.name, relay_id, other.name)
                return jsonify(error="relay_id_in_use"), 403
            relay.instance_id = relay_id
        if not relay.public_key:
            relay.public_key = public_key
            current_app.logger.info("Relay token '%s' bound to relay server %s", relay.name, relay_id)
        g.relay_public_key = public_key
        now = utcnow()
        if not relay.last_seen or not relay.online_since or (now - relay.last_seen).total_seconds() > ONLINE_GAP:
            relay.online_since = now  # (re)connected: start of a new connected period
        relay.last_seen = now
        relay.last_ip = remote_ip or None
        db.session.commit()
        g.relay = relay
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------- admin
def check_admin_credentials(username: str, password: str) -> bool:
    cfg = current_app.config
    return hmac.compare_digest(username, cfg["ADMIN_USERNAME"]) and hmac.compare_digest(
        password, cfg["ADMIN_PASSWORD"]
    )


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid CSRF token")

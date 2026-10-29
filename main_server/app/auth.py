"""Authentication helpers: relay token + IP check, admin session login, CSRF."""
import hmac
import ipaddress
import secrets
import threading
from functools import wraps

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

from .models import Relay, db, hash_token, utcnow


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
    """The machine that connected to us may relay CF-Connecting-IP: a Cloudflare
    edge, or a local/private hop (nginx on this host, cloudflared tunnel)."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private:
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


def relay_required(view):
    """Relays authenticate with `Authorization: Bearer <token>` and must call
    from one of the IPs registered for that token.

    401 -> missing/invalid token, 403 -> token valid but IP not allowed / relay disabled.
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
        # Local relay: direct connection, the IP is the real peer (proxy settings bypassed)
        remote_ip = peer_ip() if relay.local_relay else (client_ip() or "")
        if not ip_allowed(remote_ip, relay.allowed_ips):
            if not remote_ip:
                current_app.logger.warning("Relay %s refused: real IP unknown (not received through Cloudflare)", relay.name)
                return jsonify(error="ip_not_allowed", ip="unknown (not received through Cloudflare)"), 403
            current_app.logger.warning("Relay %s called from unauthorized IP %s", relay.name, remote_ip)
            return jsonify(error="ip_not_allowed", ip=remote_ip), 403
        relay.last_seen = utcnow()
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

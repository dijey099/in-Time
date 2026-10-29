"""Who is in front of the phone? Decided here, on the relay, from the snapshot the phone sends.

The phone only detects that a face is present and takes the picture; it never receives anyone's
face data and cannot claim an identity. A check-in needs a one-time token that the relay hands
out after it recognised the person itself, and the stored snapshot is the very picture used.
"""
import logging
import secrets
import threading
import time
from collections import defaultdict, deque

from .face_engine import FaceEngine, Gallery, NoFace

log = logging.getLogger("relay.identity")

TOKEN_TTL = 90          # seconds to confirm IN/OUT after being recognised
MAX_TOKENS = 500
RATE_WINDOW, RATE_MAX = 10.0, 15   # identification requests per phone (IP)


class Identifier:
    def __init__(self, engine: FaceEngine, db, min_similarity: float):
        self.engine, self.db, self.min_similarity = engine, db, min_similarity
        self.gallery = Gallery()
        self.missing_faces = []        # user ids whose photo has no usable face
        self._refresh_lock = threading.Lock()
        self._tokens = {}
        self._tokens_lock = threading.Lock()
        self._hits = defaultdict(deque)

    # ------------------------------------------------------------ gallery
    def start(self):
        """Load the gallery once the models are ready (background)."""
        def run():
            if self.engine.wait_ready():
                self.refresh()
        threading.Thread(target=run, daemon=True, name="gallery").start()

    def refresh(self):
        """Compute embeddings of new/changed photos, then reload the gallery (sync thread)."""
        if not self.engine.ok:
            return
        with self._refresh_lock:
            for u in self.db.users_needing_embedding():
                src = self.db.photo_version(u)
                emb = None
                if src:
                    try:
                        emb = self.engine.to_list(self.engine.embed(self.db.photo_path(u)))
                    except (NoFace, OSError) as exc:
                        log.warning("User %s: no usable face in the photo (%s)", u["id"], exc)
                self.db.set_embedding(u["id"], emb, src)
            users = self.db.users()
            gallery = Gallery()
            gallery.load([(u["id"], self.db.embedding(u)) for u in users])
            self.gallery = gallery
            self.missing_faces = [u["id"] for u in users if not u["embedding"]]

    # ----------------------------------------------------------- identify
    def allow(self, ip) -> bool:
        """Simple per-phone rate limit on identification requests."""
        now = time.monotonic()
        q = self._hits[ip]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True

    def identify(self, jpeg: bytes):
        """(user dict, similarity) or (None, best similarity). Raises NoFace."""
        emb = self.engine.embed(jpeg)
        user_id, sim = self.gallery.best(emb)
        if user_id is None or sim < self.min_similarity:
            return None, sim
        user = self.db.user(user_id)
        if not user or not user["active"]:
            return None, sim
        return user, sim

    # -------------------------------------------------------------- tokens
    def issue(self, user, similarity, jpeg, ip) -> str:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._tokens_lock:
            for t in [t for t, v in self._tokens.items() if v["expires"] < now]:
                del self._tokens[t]
            if len(self._tokens) >= MAX_TOKENS:
                self._tokens.pop(next(iter(self._tokens)))
            self._tokens[token] = {"user_id": user["id"], "similarity": similarity, "snapshot": jpeg,
                                   "ip": ip, "expires": now + TOKEN_TTL}
        return token

    def peek(self, token, ip):
        with self._tokens_lock:
            v = self._tokens.get(token or "")
        if not v or v["ip"] != ip or v["expires"] < time.monotonic():
            return None
        return v

    def consume(self, token, ip):
        """One-time use."""
        with self._tokens_lock:
            v = self._tokens.get(token or "")
            if not v or v["ip"] != ip or v["expires"] < time.monotonic():
                return None
            del self._tokens[token]
        return v

    # --------------------------------------------------------------- status
    @property
    def ok(self):
        return self.engine.ok

    @property
    def message(self):
        return self.engine.message

    @property
    def warning(self):
        n = len(self.missing_faces)
        if self.engine.ok and n:
            return (f"{n} user(s) can't be recognised: no photo, or no usable face in it "
                    f"({', '.join(self.missing_faces[:5])}{'…' if n > 5 else ''})")
        return ""

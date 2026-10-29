"""Server-side face recognition (OpenCV): YuNet finds the face, SFace turns it into
128 numbers ("embedding"). Two photos of the same person give a high cosine similarity.

The same file is used by the relay (identifies the person in a phone snapshot) and by the
main server (enrols user photos and re-checks every check-in snapshot). The two ONNX models
(~39 MB) are downloaded once from the OpenCV model zoo and verified with SHA-256.
"""
import hashlib
import logging
import os
import threading
import urllib.request
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # before importing cv2: hide backend notices

import cv2  # noqa: E402
import numpy as np  # noqa: E402

log = logging.getLogger("face_engine")

_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx", f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "recognizer": ("face_recognition_sface_2021dec.onnx",
                   f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                   "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
MODEL_ID = "sface-2021dec"  # stored with each embedding: embeddings of other models are not comparable

# Cosine similarity at or above which two faces are the same person (OpenCV's value for SFace)
DEFAULT_MIN_SIMILARITY = 0.363
MAX_SIDE = 960       # larger images are scaled down before detection
MIN_FACE_PX = 60     # smaller faces are too blurry to identify reliably


class NoFace(ValueError):
    """No usable face in the image."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FaceEngine:
    def __init__(self, models_dir: Path, min_similarity: float = DEFAULT_MIN_SIMILARITY):
        self.models_dir = Path(models_dir)
        self.min_similarity = min_similarity
        self.ok = False
        self.message = "Loading face recognition models…"
        self._lock = threading.Lock()  # OpenCV networks are not thread-safe
        self._detector = self._recognizer = None
        self._ready = threading.Event()

    # ------------------------------------------------------------- models
    def _missing(self):
        out = []
        for name, url, sha in MODELS.values():
            p = self.models_dir / name
            if not p.exists() or p.stat().st_size == 0:
                out.append((p, url, sha))
        return out

    def start(self):
        """Download missing models (background thread) and load them."""
        threading.Thread(target=self._prepare, daemon=True, name="face-models").start()
        return self

    def prepare(self):
        """Same as start(), but blocking (CLI commands, tests)."""
        self._prepare()
        return self.ok

    def _prepare(self):
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            for path, url, sha in self._missing():
                self.message = f"Downloading face recognition model {path.name}…"
                log.info("Downloading %s", url)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
                with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                if _sha256(tmp) != sha:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"{path.name}: checksum mismatch (corrupted or unexpected download)")
                tmp.replace(path)
            det, url, sha = MODELS["detector"]
            rec, url2, sha2 = MODELS["recognizer"]
            self._detector = cv2.FaceDetectorYN.create(str(self.models_dir / det), "", (320, 320), 0.7, 0.3, 50)
            self._recognizer = cv2.FaceRecognizerSF.create(str(self.models_dir / rec), "")
            self.ok, self.message = True, "Face recognition ready"
            log.info("Face recognition models loaded")
        except Exception as exc:
            self.ok = False
            self.message = (f"Face recognition models unavailable ({exc}). Check the internet connection and "
                            f"restart, or copy the .onnx files from another installation into {self.models_dir}.")
            log.error(self.message)
        finally:
            self._ready.set()

    def wait_ready(self, timeout=None):
        self._ready.wait(timeout)
        return self.ok

    # ---------------------------------------------------------- embeddings
    @staticmethod
    def decode(data: bytes):
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise NoFace("not a readable image")
        return img

    def embed(self, image) -> np.ndarray:
        """Embedding (unit vector, 128 floats) of the largest face. `image`: bytes, path or BGR array."""
        if not self.ok:
            raise RuntimeError(self.message)
        if isinstance(image, (bytes, bytearray)):
            img = self.decode(image)
        elif isinstance(image, (str, Path)):
            img = self.decode(Path(image).read_bytes())
        else:
            img = image
        h, w = img.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        if scale < 1:
            img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        with self._lock:
            self._detector.setInputSize((img.shape[1], img.shape[0]))
            _, faces = self._detector.detect(img)
            if faces is None or not len(faces):
                raise NoFace("no face found")
            face = max(faces, key=lambda f: f[2] * f[3])
            if min(face[2], face[3]) < MIN_FACE_PX * min(1.0, img.shape[1] / 480):
                raise NoFace("face too small: come closer to the camera")
            feat = self._recognizer.feature(self._recognizer.alignCrop(img, face)).flatten()
        return (feat / np.linalg.norm(feat)).astype(np.float32)

    @staticmethod
    def similarity(a, b) -> float:
        return float(np.dot(np.asarray(a, np.float32), np.asarray(b, np.float32)))

    @staticmethod
    def to_list(embedding):
        return [round(float(v), 6) for v in embedding]


class Gallery:
    """Embeddings of all enrolled users, for 1:N identification."""

    def __init__(self):
        self.ids, self.matrix = [], np.zeros((0, 128), np.float32)

    def load(self, items):
        """items: [(user_id, embedding list)]"""
        items = [(uid, e) for uid, e in items if e is not None and len(e) == 128]
        self.ids = [uid for uid, _ in items]
        self.matrix = np.array([e for _, e in items], np.float32).reshape(-1, 128)

    def best(self, embedding):
        """(user_id, similarity) of the closest user, or (None, 0.0) when empty."""
        if not self.ids:
            return None, 0.0
        sims = self.matrix @ np.asarray(embedding, np.float32)
        i = int(np.argmax(sims))
        return self.ids[i], float(sims[i])

"""Makes sure face-api.js and its face detector are present in relay/web/vendor.

Missing files are downloaded once from the npm registry (the official
@vladmandic/face-api package), then phones load them from the relay, offline.
"""
import io
import logging
import tarfile
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("relay.models")

VERSION = "1.7.15"
TARBALL = f"https://registry.npmjs.org/@vladmandic/face-api/-/face-api-{VERSION}.tgz"
MODELS = ["tiny_face_detector_model"]  # phones only detect that a face is there; the relay recognises it


def required_files(vendor_dir: Path):
    """(path inside the npm package, local destination)"""
    yield "package/dist/face-api.js", vendor_dir / "face-api.js"
    for m in MODELS:
        for suffix in ("-weights_manifest.json", ".bin"):
            yield f"package/model/{m}{suffix}", vendor_dir / "model" / f"{m}{suffix}"


def missing_files(vendor_dir: Path):
    return [(src, dst) for src, dst in required_files(vendor_dir) if not (dst.exists() and dst.stat().st_size > 0)]


class ModelStatus:
    def __init__(self, vendor_dir: Path):
        self.vendor_dir = vendor_dir
        self.ok = not missing_files(vendor_dir)
        self.message = "Face models ready" if self.ok else "Downloading face models…"

    def start_download(self):
        if not self.ok:
            threading.Thread(target=self._download, daemon=True, name="models").start()

    def _download(self):
        todo = missing_files(self.vendor_dir)
        try:
            log.info("Missing face model files: %s", ", ".join(d.name for _, d in todo))
            log.info("Downloading %s", TARBALL)
            with urllib.request.urlopen(TARBALL, timeout=300) as r:
                data = r.read()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for src, dst in todo:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(dst.suffix + ".part")
                    tmp.write_bytes(tar.extractfile(src).read())
                    tmp.replace(dst)
            self.ok, self.message = True, "Face models ready"
            log.info("Face models downloaded")
        except Exception as exc:
            self.message = (f"Face model files missing and download failed ({exc}). "
                            f"Check the internet connection and restart, or copy the face-api files "
                            f"from another relay into {self.vendor_dir}.")
            log.error(self.message)

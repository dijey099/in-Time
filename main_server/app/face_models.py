"""Makes sure face-api.js and its model weights are present in main_server/app/static/vendor.

Missing files are downloaded once from the npm registry (the official
@vladmandic/face-api package) and served to the admin pages.
"""
import io
import os
import logging
import tarfile
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("in_time.models")

VERSION = "1.7.15"
TARBALL = f"https://registry.npmjs.org/@vladmandic/face-api/-/face-api-{VERSION}.tgz"
MODELS = ["ssd_mobilenetv1_model", "tiny_face_detector_model", "face_landmark_68_model", "face_recognition_model"]


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
                    tmp = dst.with_suffix(dst.suffix + f".{os.getpid()}.part")
                    tmp.write_bytes(tar.extractfile(src).read())
                    tmp.replace(dst)
            self.ok, self.message = True, "Face models ready"
            log.info("Face models downloaded")
        except Exception as exc:
            self.message = (f"Face model files missing and download failed ({exc}). Check the internet "
                            f"connection and restart, or copy app/static/vendor from another installation.")
            log.error(self.message)

"""Reading and writing raw source files.

Writes go to a temporary file and are then renamed into place, so a crash or a failed
download never leaves a half-written file behind. Raw files are only overwritten when a
source is refreshed (in practice, the current season's MoneyPuck shot zip).
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_json(path: Path, obj) -> Path:
    return write_bytes(path, json.dumps(obj, indent=1).encode())


def read_json(path: Path):
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

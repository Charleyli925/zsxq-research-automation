"""Durable, attachment-bound local downloads, independent of run timestamps.

Only the dedicated download helper writes these receipts. Unknown files in
the user's staging directory are neither adopted nor removed. The payload is
installed atomically after validation; the intent precedes the browser call.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def identity(candidate: dict) -> dict:
    value = {key: str(candidate.get(key) or "").strip() for key in ("file_id", "filename", "topic_url")}
    if not all(value.values()) or Path(value["filename"]).name != value["filename"]:
        raise ValueError("download receipt requires an exact attachment identity")
    return value


def receipt_path(root: Path, candidate: dict) -> Path:
    key = hashlib.sha256(json.dumps(identity(candidate), sort_keys=True).encode()).hexdigest()
    return root / ".zsxq-downloads" / key / "receipt.json"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path, candidate: dict) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("identity") != identity(candidate) or value.get("schema_version") != 1:
        raise ValueError("download receipt identity mismatch")
    return value


def prepare(root: Path, candidate: dict) -> tuple[Path, dict]:
    path = receipt_path(root, candidate)
    value = load(path, candidate)
    if value is None:
        if path.parent.exists() and any(path.parent.iterdir()):
            raise ValueError("unowned files in download receipt directory")
        value = {"schema_version": 1, "identity": identity(candidate)}
        write(path, value)
    return path, value


def completed_path(path: Path, value: dict) -> Path | None:
    payload = path.parent / value["identity"]["filename"]
    for file in (payload, Path(value["archive_path"]) if value.get("archive_path") else None):
        if file is not None and file.is_file():
            if file.is_symlink():
                raise ValueError("download receipt must reference a regular file")
            digest = sha256(file)
            if value.get("sha256") and digest != value["sha256"]:
                raise ValueError("download receipt content mismatch")
            if not value.get("sha256"):
                # The helper's atomic rename is the only writer of this path.
                # A crash after rename but before receipt completion is safe.
                with file.open("rb") as handle:
                    if file.stat().st_size < 1024 or handle.read(5) != b"%PDF-":
                        raise ValueError("incomplete receipted PDF")
                value.update(sha256=digest, size_bytes=file.stat().st_size)
                write(path, value)
            return file
    if value.get("sha256"):
        raise ValueError("receipted PDF is missing")
    return None


def archive(path: Path, value: dict, source: Path, destination: Path) -> Path:
    """Persist destination before copying, so every crash point is recoverable."""
    destination = Path(value.get("archive_path") or destination)
    value["archive_path"] = str(destination)
    write(path, value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or sha256(destination) != value["sha256"]:
            raise ValueError("archive destination conflicts with download receipt")
    else:
        fd, name = tempfile.mkstemp(prefix=".archive-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            if sha256(temporary) != value["sha256"]:
                raise ValueError("download changed while archiving")
            os.link(temporary, destination)  # Never overwrite an unrelated file.
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
    if source != destination:
        source.unlink()
        sync_directory(source.parent)
    return destination

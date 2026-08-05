#!/usr/bin/env python3
"""Scan the ZSXQ download folder and build one batch of pending PDFs.

This script is the local watcher for the OpenClaw PDF-summary task.
It keeps a small pending queue in the state file.
New PDFs stay pending until the caller marks the batch as processed.
The OpenClaw task reads that batch file next and does the summarization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

TEMP_SUFFIXES = {".crdownload", ".download", ".part", ".partial", ".tmp", ".icloud"}
IGNORE_NAMES = {".DS_Store"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one batch of newly added ZSXQ PDFs.")
    parser.add_argument("--root", required=True, help="Root folder to scan recursively.")
    parser.add_argument(
        "--extra-root",
        action="append",
        default=[],
        help="Extra root folder to scan recursively. Can be passed more than once.",
    )
    parser.add_argument("--state-file", required=True, help="Path to the watcher state JSON.")
    parser.add_argument("--batch-file", required=True, help="Path to the output batch JSON.")
    parser.add_argument(
        "--ack-batch",
        action="store_true",
        help="Acknowledge the current batch file and move it out of pending queue.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="On the first run, include existing PDFs instead of only building a baseline.",
    )
    return parser.parse_args()


def should_ignore(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    if name in IGNORE_NAMES:
        return True
    if lower.startswith(".") or lower.startswith("~$"):
        return True
    if path.suffix.lower() != ".pdf":
        return True
    return path.suffix.lower() in TEMP_SUFFIXES


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(
    path: Path,
) -> tuple[
    dict[str, dict[str, int | str]],
    dict[str, dict[str, int | str]],
    dict[str, str],
    dict[str, str],
]:
    if not path.exists():
        return {}, {}, {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}, {}, {}
    known_files = data.get("known_files", {})
    pending_files = data.get("pending_files", {})
    known_sha256s = data.get("known_sha256s", {})
    pending_sha256s = data.get("pending_sha256s", {})
    return (
        known_files if isinstance(known_files, dict) else {},
        pending_files if isinstance(pending_files, dict) else {},
        known_sha256s if isinstance(known_sha256s, dict) else {},
        pending_sha256s if isinstance(pending_sha256s, dict) else {},
    )


def build_sha256_index(files: dict[str, dict[str, int | str]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for path_str, info in files.items():
        sha = str(info.get("sha256", "") or "").strip().lower()
        if sha:
            index[sha] = path_str
    return index


def save_state(
    path: Path,
    known_files: dict[str, dict[str, int | str]],
    pending_files: dict[str, dict[str, int | str]],
    known_sha256s: dict[str, str] | None = None,
    pending_sha256s: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "known_files": known_files,
        "pending_files": pending_files,
        "known_sha256s": known_sha256s if known_sha256s is not None else build_sha256_index(known_files),
        "pending_sha256s": pending_sha256s if pending_sha256s is not None else build_sha256_index(pending_files),
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def candidate_library_roots(roots: list[Path]) -> list[Path]:
    candidates: dict[str, Path] = {}
    for root in roots:
        root = root.expanduser().resolve()
        if root.name == "pdfs":
            candidates[str(root.parent)] = root.parent
        for path in [root, *root.parents]:
            if (path / "state" / "processed_files.sqlite").exists():
                candidates[str(path)] = path
    return list(candidates.values())


def load_index_metadata_by_sha(roots: list[Path]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for library_root in candidate_library_roots(roots):
        db_path = library_root / "state" / "processed_files.sqlite"
        if not db_path.exists():
            continue
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT report_id, pdf_sha256, title, batch_id, pdf_path
                    FROM reports
                    WHERE pdf_sha256 != ''
                    """
                ).fetchall()
        except Exception:
            continue
        for row in rows:
            sha = str(row["pdf_sha256"] or "").strip().lower()
            if not sha:
                continue
            metadata[sha] = {
                "report_id": str(row["report_id"] or "").strip(),
                "pdf_sha256": sha,
                "title": str(row["title"] or "").strip(),
                "batch_id": str(row["batch_id"] or "").strip(),
                "indexed_pdf_path": str(row["pdf_path"] or "").strip(),
            }
    return metadata


def scan_snapshot(roots: list[Path], state_path: Path) -> dict[str, dict[str, int | str]]:
    snapshot: dict[str, dict[str, int | str]] = {}
    ignored_state = str(state_path.resolve())
    metadata_by_sha = load_index_metadata_by_sha(roots)
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists() or not root.is_dir():
            continue
        for base, _, files in os.walk(root):
            for name in files:
                path = (Path(base) / name).resolve()
                if str(path) == ignored_state:
                    continue
                if should_ignore(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file():
                    continue
                try:
                    pdf_sha256 = compute_sha256(path)
                except OSError:
                    continue
                item: dict[str, int | str] = {
                    "size": int(stat.st_size),
                    "mtime": int(stat.st_mtime),
                    "root": str(root),
                    "sha256": pdf_sha256,
                }
                item.update(metadata_by_sha.get(pdf_sha256, {}))
                snapshot[str(path)] = item
    return snapshot


def relative_to_scan_root(path: Path, root: Path, info: dict[str, int | str]) -> str:
    raw_root = str(info.get("root", "")).strip()
    candidate_roots = [Path(raw_root).expanduser()] if raw_root else []
    candidate_roots.append(root)
    for candidate_root in candidate_roots:
        try:
            return path.relative_to(candidate_root.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def batch_id_from_scan_path(path: Path, root: Path, info: dict[str, int | str]) -> str:
    existing = str(info.get("batch_id", "") or "").strip()
    if existing:
        return existing
    relative = relative_to_scan_root(path, root, info)
    parts = Path(relative).parts
    if len(parts) >= 2:
        return parts[0]
    parent_name = path.parent.name
    if "__to__" in parent_name:
        return parent_name
    return ""


def display_filename(path: Path, info: dict[str, int | str]) -> str:
    value = str(info.get("title", "") or path.name).strip()
    if not value:
        value = path.name
    if value.lower().endswith(".pdf"):
        return value
    return f"{value}.pdf"


def build_batch(
    root: Path,
    roots: list[Path],
    batch_path: Path,
    state_path: Path,
    pending_files: dict[str, dict[str, int | str]],
    first_run: bool,
) -> None:
    files = []
    for path_str in sorted(pending_files.keys(), key=lambda item: pending_files[item]["mtime"]):
        path = Path(path_str)
        info = pending_files[path_str]
        files.append(
            {
                "path": path_str,
                "filename": display_filename(path, info),
                "stored_filename": path.name,
                "relative_path": relative_to_scan_root(path, root, info),
                "size_bytes": info["size"],
                "modified_at": datetime.fromtimestamp(info["mtime"]).astimezone().isoformat(),
                "scan_root": str(info.get("root", "")) or str(root),
                "pdf_sha256": str(info.get("sha256", "") or info.get("pdf_sha256", "")),
                "report_id": str(info.get("report_id", "")),
                "title": str(info.get("title", "")),
                "batch_id": batch_id_from_scan_path(path, root, info),
            }
        )

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "root": str(root),
        "roots": [str(item) for item in roots],
        "state_file": str(state_path),
        "batch_file": str(batch_path),
        "first_run": first_run,
        "new_pdf_count": len(files),
        "latest_modified_at": files[-1]["modified_at"] if files else None,
        "files": files,
    }
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def acknowledge_batch(
    state_path: Path,
    batch_path: Path,
    known_files: dict[str, dict[str, int | str]],
    pending_files: dict[str, dict[str, int | str]],
    known_sha256s: dict[str, str] | None = None,
    pending_sha256s: dict[str, str] | None = None,
) -> dict[str, int]:
    known_sha256s = known_sha256s if known_sha256s is not None else build_sha256_index(known_files)
    pending_sha256s = pending_sha256s if pending_sha256s is not None else build_sha256_index(pending_files)
    if not batch_path.exists():
        save_state(state_path, known_files, pending_files, known_sha256s, pending_sha256s)
        return {"acked_count": 0}

    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except Exception:
        save_state(state_path, known_files, pending_files, known_sha256s, pending_sha256s)
        return {"acked_count": 0}

    acked_count = 0
    for item in batch.get("files", []):
        path_str = str(item.get("path", "")).strip()
        if not path_str:
            continue
        entry = pending_files.pop(path_str, None)
        if entry is None:
            entry = {
                "size": int(item.get("size_bytes", 0) or 0),
                "mtime": int(Path(path_str).stat().st_mtime) if Path(path_str).exists() else int(time.time()),
                "root": str(item.get("scan_root", "")),
                "sha256": str(item.get("pdf_sha256", "") or ""),
            }
        elif "root" not in entry and str(item.get("scan_root", "")).strip():
            entry["root"] = str(item.get("scan_root", ""))
        if "sha256" not in entry and str(item.get("pdf_sha256", "")).strip():
            entry["sha256"] = str(item.get("pdf_sha256", "")).strip()
        known_files[path_str] = entry
        sha = str(entry.get("sha256", "") or "").strip().lower()
        if sha:
            pending_sha256s.pop(sha, None)
            known_sha256s[sha] = path_str
        acked_count += 1

    save_state(state_path, known_files, pending_files, known_sha256s, pending_sha256s)
    return {"acked_count": acked_count}


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    roots = [root, *[Path(value).expanduser().resolve() for value in args.extra_root]]
    roots = list(dict.fromkeys(roots))
    state_path = Path(args.state_file).expanduser().resolve()
    batch_path = Path(args.batch_file).expanduser().resolve()

    first_run = not state_path.exists()
    known_files, pending_files, known_sha256s, pending_sha256s = load_state(state_path)
    known_sha256s = {**build_sha256_index(known_files), **{str(k).lower(): str(v) for k, v in known_sha256s.items()}}
    pending_sha256s = {**build_sha256_index(pending_files), **{str(k).lower(): str(v) for k, v in pending_sha256s.items()}}

    if args.ack_batch:
        summary = acknowledge_batch(state_path, batch_path, known_files, pending_files, known_sha256s, pending_sha256s)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    existing_roots = [item for item in roots if item.exists() and item.is_dir()]
    if not existing_roots:
        raise SystemExit(f"scan root not found: {root}")

    current = scan_snapshot(roots, state_path)

    if first_run and not args.include_existing:
        known_files = current.copy()
        pending_files = {}
        known_sha256s = build_sha256_index(known_files)
        pending_sha256s = {}
    else:
        for path in list(known_files.keys()):
            if path not in current:
                continue
            known_files[path] = {
                **known_files[path],
                "size": current[path]["size"],
                "mtime": current[path]["mtime"],
                "root": current[path].get("root", ""),
                "sha256": current[path].get("sha256", ""),
                "report_id": current[path].get("report_id", known_files[path].get("report_id", "")),
                "title": current[path].get("title", known_files[path].get("title", "")),
                "batch_id": current[path].get("batch_id", known_files[path].get("batch_id", "")),
            }
            sha = str(known_files[path].get("sha256", "") or "").strip().lower()
            if sha:
                known_sha256s[sha] = path
        pending_files = {
            path: {
                "size": current[path]["size"],
                "mtime": current[path]["mtime"],
                "root": current[path].get("root", ""),
                "sha256": current[path].get("sha256", ""),
                "report_id": current[path].get("report_id", ""),
                "title": current[path].get("title", ""),
                "batch_id": current[path].get("batch_id", ""),
            }
            for path in pending_files.keys()
            if path in current
        }
        pending_sha256s = build_sha256_index(pending_files)
        for path in current.keys():
            if path in known_files or path in pending_files:
                continue
            sha = str(current[path].get("sha256", "") or "").strip().lower()
            if sha and sha in known_sha256s:
                known_files[path] = current[path]
                known_sha256s[sha] = path
                continue
            if sha and sha in pending_sha256s:
                pending_files[path] = current[path]
                pending_sha256s[sha] = path
                continue
            pending_files[path] = {
                "size": current[path]["size"],
                "mtime": current[path]["mtime"],
                "root": current[path].get("root", ""),
                "sha256": current[path].get("sha256", ""),
                "report_id": current[path].get("report_id", ""),
                "title": current[path].get("title", ""),
                "batch_id": current[path].get("batch_id", ""),
            }
            if sha:
                pending_sha256s[sha] = path

    save_state(state_path, known_files, pending_files, known_sha256s, pending_sha256s)
    build_batch(root, roots, batch_path, state_path, pending_files, first_run)

    summary = {
        "first_run": first_run,
        "new_pdf_count": len(pending_files),
        "batch_file": str(batch_path),
        "root": str(root),
        "roots": [str(item) for item in roots],
        "scanned_file_count": len(current),
        "updated_at": int(time.time()),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Repair changed, missing, and removed ResearchVault full-text entries."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_VAULT_ROOT,
    ensure_search_table,
    extract_report_metadata,
    load_kb_configs,
    load_note,
    upsert_metadata,
    upsert_search_note,
)


def _file_signature(path: str) -> tuple[int, int]:
    if not path:
        return 0, 0
    try:
        stat = Path(path).stat()
    except OSError:
        return 0, 0
    return stat.st_mtime_ns, stat.st_size


def reconcile(db_path: Path, vault_root: Path, config_root: Path, *, max_updates: int = 250) -> dict[str, int | bool | str]:
    if max_updates < 1:
        raise ValueError("max_updates must be positive")
    report_root = vault_root / "10_Reports"
    if not report_root.is_dir():
        raise FileNotFoundError(f"report directory unavailable: {report_root}")
    with closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        with conn:
            ensure_search_table(conn)
        states = {
            row[1]: row
            for row in conn.execute(
                "SELECT report_id, note_path, note_mtime_ns, note_size, summary_path, summary_mtime_ns, summary_size FROM report_search_state"
            )
        }
        indexed_paths = {row[0] for row in conn.execute("SELECT note_path FROM report_search")}

    configs = load_kb_configs(config_root)
    seen_paths: set[str] = set()
    updated = pending = errors = 0
    paths = list(report_root.rglob("*.md"))
    # Repair genuinely missing search rows before seeding signatures for rows
    # inherited from an older full rebuild.
    paths.sort(key=lambda path: (str(path) in indexed_paths, str(path)))
    for path in paths:
        path_text = str(path)
        seen_paths.add(path_text)
        state = states.get(path_text)
        try:
            note_signature = _file_signature(path_text)
            if note_signature == (0, 0):
                raise OSError(f"cannot stat report note: {path_text}")
            summary_signature = _file_signature(state[4]) if state else (0, 0)
            if (
                state
                and path_text in indexed_paths
                and note_signature == (state[2], state[3])
                and summary_signature == (state[5], state[6])
            ):
                continue
            pending += 1
            if updated >= max_updates:
                continue
            note = load_note(path, vault_root)
            if not note.link_target.startswith("10_Reports/"):
                raise ValueError(f"report note has invalid vault target: {path}")
            metadata = extract_report_metadata(note, configs, vault_root)
            if path_text not in indexed_paths or state is not None:
                upsert_metadata(db_path, note, metadata, "kb_reconcile_search")
            upsert_search_note(db_path, note, config_root, vault_root, metadata=metadata)
            updated += 1
        except (OSError, ValueError, sqlite3.Error):
            errors += 1

    stale_paths = {path for path in states if path not in seen_paths}
    stale_paths.update(path for path in indexed_paths if path and not Path(path).is_file())
    with closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        with conn:
            for path in stale_paths:
                conn.execute("DELETE FROM report_search WHERE note_path = ?", (path,))
                conn.execute("DELETE FROM report_search_state WHERE note_path = ?", (path,))
        indexed, latest_date = conn.execute(
            """SELECT count(*), max(CASE
              WHEN report_date BETWEEN '2000-01-01' AND date('now', 'localtime')
                AND date(report_date) IS NOT NULL THEN report_date ELSE NULL END)
              FROM report_search"""
        ).fetchone()
    return {
        "seen": len(seen_paths), "updated": updated, "pending": pending - updated,
        "deleted": len(stale_paths), "errors": errors,
        "indexed": indexed, "latest_report_date": latest_date or "",
        "complete": pending == updated and errors == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--max-updates", type=int, default=250)
    args = parser.parse_args()
    result = reconcile(
        Path(args.db_path).expanduser().resolve(strict=False),
        Path(args.vault_root).expanduser().resolve(strict=False),
        Path(args.config_root).expanduser().resolve(strict=False),
        max_updates=args.max_updates,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

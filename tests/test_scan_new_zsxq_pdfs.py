"""
这个文件测试待处理 PDF 队列的确认逻辑。

它测试的是 `scripts/scan_new_zsxq_pdfs.py`。
这个脚本会维护两份状态：
- `pending_files`：还没总结的文件
- `known_files`：已经确认过的文件
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scan_new_zsxq_pdfs.py"
SPEC = importlib.util.spec_from_file_location("scan_new_zsxq_pdfs", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ScanNewZsxqPdfsTests(unittest.TestCase):
    def test_scan_snapshot_collects_extra_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            old_root = tmp_path / "old"
            new_root = tmp_path / "new"
            state_path = tmp_path / "state.json"
            old_root.mkdir()
            new_root.mkdir()
            old_pdf = old_root / "old.pdf"
            new_pdf = new_root / "nested" / "new.pdf"
            new_pdf.parent.mkdir()
            old_pdf.write_bytes(b"pdf")
            new_pdf.write_bytes(b"pdf")

            snapshot = MODULE.scan_snapshot([old_root, new_root], state_path)

            self.assertIn(str(old_pdf.resolve()), snapshot)
            self.assertIn(str(new_pdf.resolve()), snapshot)
            self.assertEqual(snapshot[str(new_pdf.resolve())]["root"], str(new_root.resolve()))
            self.assertEqual(len(str(snapshot[str(new_pdf.resolve())]["sha256"])), 64)

    def test_scan_snapshot_enriches_research_library_pdf_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            pdf_root = library_root / "pdfs"
            db_path = library_root / "state" / "processed_files.sqlite"
            pdf_root.mkdir(parents=True)
            db_path.parent.mkdir(parents=True)
            pdf_path = pdf_root / "2026" / "05" / "zsxq_demo.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-indexed")
            sha = MODULE.compute_sha256(pdf_path)
            with closing(sqlite3.connect(str(db_path))) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE reports (
                          report_id TEXT PRIMARY KEY,
                          pdf_sha256 TEXT NOT NULL DEFAULT '',
                          title TEXT NOT NULL DEFAULT '',
                          batch_id TEXT NOT NULL DEFAULT '',
                          pdf_path TEXT NOT NULL DEFAULT ''
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO reports(report_id, pdf_sha256, title, batch_id, pdf_path)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("zsxq_demo", sha, "腾讯深度报告", "batch-1", str(pdf_path)),
                    )

            snapshot = MODULE.scan_snapshot([pdf_root], library_root / "state" / "watch_state.json")
            info = snapshot[str(pdf_path.resolve())]

            self.assertEqual(info["report_id"], "zsxq_demo")
            self.assertEqual(info["title"], "腾讯深度报告")

    def test_acknowledge_batch_moves_pending_files_to_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            batch_path = tmp_path / "batch.json"
            pdf_path = tmp_path / "a.pdf"
            pdf_path.write_bytes(b"pdf")

            known_files = {}
            pending_files = {
                str(pdf_path): {
                    "size": 3,
                    "mtime": 123,
                }
            }
            batch = {
                "files": [
                    {
                        "path": str(pdf_path),
                        "size_bytes": 3,
                    }
                ]
            }
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")

            result = MODULE.acknowledge_batch(state_path, batch_path, known_files, pending_files)

            self.assertEqual(result["acked_count"], 1)
            self.assertNotIn(str(pdf_path), pending_files)
            self.assertEqual(known_files[str(pdf_path)]["size"], 3)

    def test_acknowledge_batch_uses_batch_file_when_pending_entry_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            batch_path = tmp_path / "batch.json"
            pdf_path = tmp_path / "b.pdf"
            pdf_path.write_bytes(b"pdf")

            known_files = {}
            pending_files = {}
            batch = {
                "files": [
                    {
                        "path": str(pdf_path),
                        "size_bytes": 3,
                    }
                ]
            }
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")

            result = MODULE.acknowledge_batch(state_path, batch_path, known_files, pending_files)

            self.assertEqual(result["acked_count"], 1)
            self.assertEqual(known_files[str(pdf_path)]["size"], 3)
            self.assertIsInstance(known_files[str(pdf_path)]["mtime"], int)
            self.assertEqual(pending_files, {})

    def test_same_pdf_in_new_root_is_not_requeued_by_path_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            old_root = tmp_path / "old"
            new_root = tmp_path / "new"
            state_path = tmp_path / "state.json"
            batch_path = tmp_path / "batch.json"
            old_root.mkdir()
            new_root.mkdir()
            old_pdf = old_root / "report.pdf"
            new_pdf = new_root / "zsxq_same.pdf"
            old_pdf.write_bytes(b"%PDF-same-content")
            new_pdf.write_bytes(b"%PDF-same-content")
            sha = MODULE.compute_sha256(old_pdf)
            state_path.write_text(
                json.dumps(
                    {
                        "known_files": {
                            str(old_pdf.resolve()): {
                                "size": old_pdf.stat().st_size,
                                "mtime": int(old_pdf.stat().st_mtime),
                                "root": str(old_root.resolve()),
                                "sha256": sha,
                            }
                        },
                        "pending_files": {},
                        "known_sha256s": {sha: str(old_pdf.resolve())},
                        "pending_sha256s": {},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--root",
                    str(old_root),
                    "--extra-root",
                    str(new_root),
                    "--state-file",
                    str(state_path),
                    "--batch-file",
                    str(batch_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            summary = json.loads(completed.stdout)
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["new_pdf_count"], 0)
            self.assertEqual(batch["files"], [])
            self.assertIn(sha, state["known_sha256s"])
            self.assertEqual(state["pending_sha256s"], {})

    def test_quiet_window_keeps_recent_pdf_pending_without_emitting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            root = tmp_path / "pdfs"
            root.mkdir()
            recent = root / "recent.pdf"
            older = root / "older.pdf"
            recent.write_bytes(b"recent")
            older.write_bytes(b"older")
            batch_path = tmp_path / "batch.json"
            now_epoch = 1_700_000_000
            pending = {
                str(recent): {"size": 6, "mtime": now_epoch - 30, "sha256": "a" * 64, "root": str(root)},
                str(older): {"size": 5, "mtime": now_epoch - 901, "sha256": "b" * 64, "root": str(root)},
            }

            eligible, deferred = MODULE.build_batch(
                root,
                [root],
                batch_path,
                tmp_path / "state.json",
                pending,
                False,
                quiet_window_minutes=15,
                now_epoch=now_epoch,
            )

            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            self.assertEqual((eligible, deferred), (1, 1))
            self.assertEqual([item["filename"] for item in batch["files"]], ["older.pdf"])
            self.assertEqual(batch["pending_pdf_count"], 2)
            self.assertEqual(batch["deferred_recent_count"], 1)


if __name__ == "__main__":
    unittest.main()

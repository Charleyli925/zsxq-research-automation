from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "research_library_index.py"
SPEC = importlib.util.spec_from_file_location("research_library_index", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ResearchLibraryIndexTests(unittest.TestCase):
    def test_connect_sets_busy_timeout_for_parallel_summary_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)

            with closing(MODULE.connect(db_path)) as conn:
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

            self.assertEqual(busy_timeout, 30000)

    def test_upsert_report_merges_metadata_by_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)
            sha = "a" * 64

            first = MODULE.upsert_report(
                db_path,
                {
                    "pdf_sha256": sha,
                    "title": "原始标题",
                    "pdf_path": "/tmp/a.pdf",
                    "index_status": "pdf_archived",
                },
            )
            second = MODULE.upsert_report(
                db_path,
                {
                    "pdf_sha256": sha,
                    "summary_md_path": "/tmp/a.summary.md",
                    "index_status": "summary_created",
                },
            )

            self.assertEqual(first["report_id"], "zsxq_aaaaaaaaaaaaaaaa")
            self.assertEqual(second["title"], "原始标题")
            self.assertEqual(second["summary_md_path"], "/tmp/a.summary.md")
            self.assertEqual(second["index_status"], "summary_created")

    def test_successful_upsert_clears_previous_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)
            sha = "b" * 64

            MODULE.upsert_report(
                db_path,
                {
                    "pdf_sha256": sha,
                    "index_status": "clean_md_failed",
                    "error_message": "clean markdown too short",
                },
            )
            second = MODULE.upsert_report(
                db_path,
                {
                    "pdf_sha256": sha,
                    "index_status": "clean_md_created",
                    "error_message": "",
                },
            )

            self.assertEqual(second["error_message"], "")
            self.assertEqual(second["index_status"], "clean_md_created")

    def test_record_event_appends_trace_without_changing_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)
            sha = "c" * 64

            MODULE.upsert_report(
                db_path,
                {
                    "pdf_sha256": sha,
                    "title": "事件测试",
                    "index_status": "pdf_archived",
                },
            )
            event = MODULE.record_event(
                db_path,
                {
                    "pdf_sha256": sha,
                    "status": "summary_created",
                    "artifact_path": "/tmp/c.summary.md",
                },
            )

            with closing(sqlite3.connect(str(db_path))) as conn:
                row = conn.execute(
                    "SELECT report_id, status, stage, artifact_path FROM report_events WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()
                report = conn.execute(
                    "SELECT title, index_status FROM reports WHERE pdf_sha256 = ?",
                    (sha,),
                ).fetchone()

            self.assertEqual(row, ("zsxq_cccccccccccccccc", "summary_created", "summary", "/tmp/c.summary.md"))
            self.assertEqual(report, ("事件测试", "pdf_archived"))

    def test_record_text_extract_events_only_records_existing_extractor_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)
            batch_file = Path(tmp_dir) / "batch.json"
            clean_path = Path(tmp_dir) / "clean.md"
            clean_path.write_text("clean text", encoding="utf-8")
            batch_file.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "report_id": "zsxq_markitdownclean",
                                "pdf_sha256": "d" * 64,
                                "batch_id": "batch-1",
                                "text_extract_status": "success",
                                "text_source": "markitdown_clean",
                                "extracted_text_path": str(clean_path),
                            },
                            {
                                "report_id": "zsxq_fallbacksuccess",
                                "pdf_sha256": "e" * 64,
                                "batch_id": "batch-1",
                                "text_extract_status": "success",
                                "text_source": "pdftotext_fastpath",
                                "extracted_text_path": "/tmp/fallback.txt",
                            },
                            {
                                "report_id": "zsxq_extractfailed",
                                "pdf_sha256": "f" * 64,
                                "batch_id": "batch-1",
                                "text_extract_status": "failed",
                                "text_extract_error": "no usable text",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            events = MODULE.record_text_extract_events_from_batch(db_path, batch_file)

            self.assertEqual([event["status"] for event in events], ["text_extract_fallback_success", "text_extract_failed"])
            with closing(sqlite3.connect(str(db_path))) as conn:
                rows = conn.execute(
                    "SELECT report_id, status FROM report_events ORDER BY created_at, event_id"
                ).fetchall()

            self.assertEqual(
                rows,
                [
                    ("zsxq_fallbacksuccess", "text_extract_fallback_success"),
                    ("zsxq_extractfailed", "text_extract_failed"),
                ],
            )

    def test_record_text_extract_started_events_skips_usable_markitdown_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_root = Path(tmp_dir) / "ResearchLibrary"
            db_path = MODULE.db_path_for_library(library_root)
            batch_file = Path(tmp_dir) / "batch.json"
            clean_path = Path(tmp_dir) / "clean.md"
            clean_path.write_text("clean text", encoding="utf-8")
            batch_file.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "report_id": "zsxq_markitdownclean",
                                "pdf_sha256": "d" * 64,
                                "batch_id": "batch-1",
                                "text_extract_status": "success",
                                "text_source": "markitdown_clean",
                                "extracted_text_path": str(clean_path),
                            },
                            {
                                "report_id": "zsxq_needsfallback",
                                "pdf_sha256": "e" * 64,
                                "batch_id": "batch-1",
                                "path": "/tmp/fallback.pdf",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            events = MODULE.record_text_extract_started_events_from_batch(db_path, batch_file)

            self.assertEqual([event["report_id"] for event in events], ["zsxq_needsfallback"])
            self.assertEqual(events[0]["status"], "text_extract_fallback_started")


if __name__ == "__main__":
    unittest.main()

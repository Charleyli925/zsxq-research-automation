from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "finalize_download_batch.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("finalize_download_batch", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_run_fixture(tmp_path: Path, filenames: list[str]) -> tuple[Path, Path, Path, Path, Path, Path]:
    staging_dir = tmp_path / "downloads"
    archive_root = tmp_path / "archive"
    staging_dir.mkdir()
    archive_root.mkdir()
    config_path = tmp_path / "job.json"
    config_path.write_text(
        json.dumps(
            {
                "download_settings": {
                    "staging_dir": str(staging_dir),
                    "archive_root": str(archive_root),
                    "allowed_extensions": [".pdf"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    keywords_path = tmp_path / "keywords.json"
    keywords_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "standalone_keywords": ["报告"],
                "region_keywords": [],
                "region_required_keywords": [],
                "exclude_keywords": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"last_successful_check_at": "2026-07-14T08:00:00+08:00"}),
        encoding="utf-8",
    )
    plan_path = tmp_path / "scan-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "window_start": "2026-07-14T08:00:00+08:00",
                "window_end": "2026-07-14T12:00:00+08:00",
                "scan_mode": "api_first",
                "api_probe_status": "ok",
                "blocked_reason": None,
                "window_new_docs_count": len(filenames),
                "keyword_matched_docs_count": len(filenames),
                "download_candidate_count": len(filenames),
                "download_candidates": [
                    {"file_id": f"file-{index}", "filename": filename, "topic_id": f"topic-{index}"}
                    for index, filename in enumerate(filenames, 1)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return staging_dir, archive_root, config_path, keywords_path, state_path, plan_path


class FinalizeDownloadBatchTests(unittest.TestCase):
    def test_discover_candidates_uses_new_standalone_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            archive_root.mkdir()
            pdf_path = staging_dir / "腾讯深度报告.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            candidates, rejected = MODULE.discover_candidates(
                staging_dir=staging_dir,
                extra_staging_dirs=[],
                allowed_extensions={".pdf"},
                keywords_payload={
                    "schema_version": 2,
                    "standalone_keywords": ["腾讯"],
                    "region_keywords": ["中国"],
                    "region_required_keywords": ["OTC"],
                    "exclude_keywords": [],
                },
                window_start=datetime.fromisoformat("2026-04-07T08:00:00+08:00"),
                window_end=datetime.now().astimezone(),
                archive_root=archive_root,
                downloaded_after=None,
                downloaded_before=None,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].match_rule, "standalone")
            self.assertEqual(candidates[0].matched_keywords, ["腾讯"])
            self.assertEqual(rejected, [])

    def test_discover_candidates_keeps_region_plus_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            archive_root.mkdir()
            pdf_path = staging_dir / "中国OTC市场周报.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            candidates, rejected = MODULE.discover_candidates(
                staging_dir=staging_dir,
                extra_staging_dirs=[],
                allowed_extensions={".pdf"},
                keywords_payload={
                    "schema_version": 2,
                    "standalone_keywords": ["腾讯"],
                    "region_keywords": ["中国"],
                    "region_required_keywords": ["OTC"],
                    "exclude_keywords": [],
                },
                window_start=datetime.fromisoformat("2026-04-07T08:00:00+08:00"),
                window_end=datetime.now().astimezone(),
                archive_root=archive_root,
                downloaded_after=None,
                downloaded_before=None,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].match_rule, "region_plus_topic")
            self.assertEqual(candidates[0].matched_keywords, ["OTC", "中国"])
            self.assertEqual(rejected, [])

    def test_skip_state_update_archives_files_without_advancing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            archive_root.mkdir()

            pdf_path = staging_dir / "腾讯深度报告.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            keywords_path = tmp_path / "keywords.json"
            keywords_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "standalone_keywords": ["腾讯"],
                        "region_keywords": [],
                        "region_required_keywords": [],
                        "exclude_keywords": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp_path / "job.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download_settings": {
                            "staging_dir": str(staging_dir),
                            "archive_root": str(archive_root),
                            "allowed_extensions": [".pdf"],
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            state_path = tmp_path / "state.json"
            original_state = {
                "last_successful_check_at": "2026-04-08T12:02:28.794761+08:00",
                "last_window_start": "2026-04-08T08:01:33.430140+08:00",
                "last_window_end": "2026-04-08T12:02:28.794761+08:00",
                "last_batch_dir": None,
                "last_batch_file_count": 0,
                "last_batch_files": [],
                "last_run_summary": "Archived 0 file(s).",
            }
            state_path.write_text(
                json.dumps(original_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--config",
                    str(config_path),
                    "--keywords",
                    str(keywords_path),
                    "--state",
                    str(state_path),
                    "--window-start",
                    "2026-04-08T12:02:28.794761+08:00",
                    "--window-end",
                    "2026-04-08T16:12:25+08:00",
                    "--downloaded-after",
                    "2026-04-08T16:00:00+08:00",
                    "--skip-state-update",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["file_count"], 1)
            self.assertFalse(summary["state_updated"])
            self.assertFalse(pdf_path.exists())
            self.assertTrue(Path(summary["batch_dir"]).exists())
            self.assertTrue((Path(summary["batch_dir"]) / "腾讯深度报告.pdf").exists())
            manifest = json.loads((Path(summary["batch_dir"]) / "batch_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("report_id", manifest["files"][0])
            self.assertIn("pdf_sha256", manifest["files"][0])

            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_state, original_state)

    def test_research_library_layout_archives_pdf_by_batch_and_original_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            library_root = tmp_path / "ResearchLibrary"
            archive_root = library_root / "pdfs"
            staging_dir.mkdir()
            pdf_path = staging_dir / "腾讯深度报告.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            candidate = MODULE.CandidateFile(
                path=pdf_path,
                archive_name=pdf_path.name,
                modified_at=datetime.fromisoformat("2026-05-07T10:00:00+08:00"),
                matched_keywords=["腾讯"],
                match_rule="standalone",
                size_bytes=pdf_path.stat().st_size,
                source_priority=0,
            )

            batch_dir, manifest = MODULE.archive_candidates(
                candidates=[candidate],
                archive_root=archive_root,
                batch_dir_name="batch-1",
                dry_run=False,
                library_root=library_root,
            )

            self.assertEqual(batch_dir, archive_root / "batch-1")
            self.assertEqual(len(manifest), 1)
            archived_path = Path(manifest[0]["path"])
            self.assertEqual(archived_path.parent, archive_root / "batch-1")
            self.assertEqual(archived_path.name, "腾讯深度报告.pdf")
            self.assertEqual(manifest[0]["filename"], "腾讯深度报告.pdf")
            self.assertEqual(manifest[0]["title"], "腾讯深度报告")
            self.assertFalse(pdf_path.exists())
            self.assertTrue(archived_path.exists())

    def test_discover_candidates_prefers_valid_playwright_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            mcp_dir = tmp_path / ".playwright-mcp"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            mcp_dir.mkdir()
            archive_root.mkdir()

            empty_copy = staging_dir / "野村证券-三星电子（005930.KS）26年一季度大幅超预期；内存超级周期持续-260407.pdf"
            mcp_copy = mcp_dir / "野村证券-三星电子（005930-KS）26年一季度大幅超预期；内存超级周期持续-260407.pdf"
            empty_copy.write_bytes(b"")
            mcp_copy.write_bytes(b"%PDF-test")

            candidates, rejected = MODULE.discover_candidates(
                staging_dir=staging_dir,
                extra_staging_dirs=[mcp_dir],
                allowed_extensions={".pdf"},
                keywords_payload={
                    "schema_version": 2,
                    "standalone_keywords": ["内存"],
                    "region_keywords": [],
                    "region_required_keywords": [],
                    "exclude_keywords": [],
                },
                window_start=datetime.fromisoformat("2026-04-07T08:00:00+08:00"),
                window_end=datetime.now().astimezone(),
                archive_root=archive_root,
                downloaded_after=None,
                downloaded_before=None,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].path, mcp_copy)
            self.assertEqual(candidates[0].archive_name, empty_copy.name)
            self.assertEqual(rejected, [])

    def test_discover_candidates_rejects_empty_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            archive_root.mkdir()

            empty_pdf = staging_dir / "腾讯深度报告.pdf"
            empty_pdf.write_bytes(b"")

            candidates, rejected = MODULE.discover_candidates(
                staging_dir=staging_dir,
                extra_staging_dirs=[],
                allowed_extensions={".pdf"},
                keywords_payload={
                    "schema_version": 2,
                    "standalone_keywords": ["腾讯"],
                    "region_keywords": [],
                    "region_required_keywords": [],
                    "exclude_keywords": [],
                },
                window_start=datetime.fromisoformat("2026-04-07T08:00:00+08:00"),
                window_end=datetime.now().astimezone(),
                archive_root=archive_root,
                downloaded_after=None,
                downloaded_before=None,
            )

            self.assertEqual(candidates, [])
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["filename"], "腾讯深度报告.pdf")
            self.assertEqual(rejected[0]["reason"], "empty_file")

    def test_discover_candidates_skips_variant_name_when_archive_already_has_same_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            staging_dir = tmp_path / "downloads"
            mcp_dir = tmp_path / ".playwright-mcp"
            archive_root = tmp_path / "archive"
            staging_dir.mkdir()
            mcp_dir.mkdir()
            archive_root.mkdir()

            archived_pdf = archive_root / "野村证券-三星电子（005930.KS）26年一季度大幅超预期；内存超级周期持续-260407.pdf"
            leftover_variant = mcp_dir / "野村证券-三星电子（005930-KS）26年一季度大幅超预期；内存超级周期持续-260407.pdf"
            archived_pdf.write_bytes(b"%PDF-test")
            leftover_variant.write_bytes(b"%PDF-test")

            candidates, rejected = MODULE.discover_candidates(
                staging_dir=staging_dir,
                extra_staging_dirs=[mcp_dir],
                allowed_extensions={".pdf"},
                keywords_payload={
                    "schema_version": 2,
                    "standalone_keywords": ["内存"],
                    "region_keywords": [],
                    "region_required_keywords": [],
                    "exclude_keywords": [],
                },
                window_start=datetime.fromisoformat("2026-04-07T08:00:00+08:00"),
                window_end=datetime.now().astimezone(),
                archive_root=archive_root,
                downloaded_after=None,
                downloaded_before=None,
            )

            self.assertEqual(candidates, [])
            self.assertEqual(rejected, [])

    def test_run_manifest_aggregates_three_plus_one_and_commits_frozen_scan_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            filenames = [f"报告{i}.pdf" for i in range(1, 5)]
            staging, _, config, keywords, state, plan = write_run_fixture(tmp_path, filenames)
            manifest = tmp_path / "run.json"
            run_id = "11111111-1111-4111-8111-111111111111"
            base_command = [
                sys.executable,
                str(SCRIPT_PATH),
                "--config", str(config),
                "--keywords", str(keywords),
                "--state", str(state),
                "--window-start", "2026-07-14T08:00:00+08:00",
                "--window-end", "2026-07-14T12:00:00+08:00",
                "--downloaded-after", "2026-07-14T00:00:00+08:00",
                "--run-id", run_id,
                "--scan-plan", str(plan),
                "--run-manifest", str(manifest),
            ]

            for filename in filenames[:3]:
                (staging / filename).write_bytes(b"%PDF-first")
            unexpected = staging / "报告-不在扫描计划.pdf"
            unexpected.write_bytes(b"%PDF-unexpected")
            first = subprocess.run([*base_command, "--skip-state-update"], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(first.stdout)["file_count"], 3)
            self.assertTrue(unexpected.exists())
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["last_successful_check_at"], "2026-07-14T08:00:00+08:00")

            (staging / filenames[3]).write_bytes(b"%PDF-second")
            second = subprocess.run([*base_command, "--commit-state"], check=True, capture_output=True, text=True)
            summary = json.loads(second.stdout)
            saved_state = json.loads(state.read_text(encoding="utf-8"))
            saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))

            self.assertEqual(summary["attempt_file_count"], 1)
            self.assertEqual(summary["file_count"], 4)
            self.assertCountEqual(summary["files"], filenames)
            self.assertEqual(len(summary["archive_dirs"]), 1)
            self.assertTrue(summary["state_updated"])
            self.assertEqual(saved_state["last_successful_check_at"], "2026-07-14T12:00:00+08:00")
            self.assertEqual(saved_state["last_batch_file_count"], 4)
            self.assertEqual(saved_manifest["missing_candidate_count"], 0)

    def test_partial_run_never_advances_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            filenames = ["报告A.pdf", "报告B.pdf"]
            staging, _, config, keywords, state, plan = write_run_fixture(tmp_path, filenames)
            manifest = tmp_path / "run.json"
            (staging / filenames[0]).write_bytes(b"%PDF-only-one")

            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT_PATH),
                    "--config", str(config), "--keywords", str(keywords), "--state", str(state),
                    "--window-start", "2026-07-14T08:00:00+08:00",
                    "--window-end", "2026-07-14T12:00:00+08:00",
                    "--downloaded-after", "2026-07-14T00:00:00+08:00",
                    "--run-id", "22222222-2222-4222-8222-222222222222",
                    "--scan-plan", str(plan), "--run-manifest", str(manifest),
                    "--commit-state",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 3)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["last_successful_check_at"], "2026-07-14T08:00:00+08:00")
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["missing_candidate_count"], 1)

    def test_content_duplicate_satisfies_plan_without_counting_as_new_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            filename = "瑞银-紫金矿业完整长标题报告.pdf"
            staging, archive_root, config, keywords, state, plan = write_run_fixture(tmp_path, [filename])
            library_root = tmp_path / "library"
            library_pdf_root = library_root / "pdfs"
            library_pdf_root.mkdir(parents=True)
            existing = library_pdf_root / "old-batch" / "瑞银-紫金矿业截断标题.pdf"
            existing.parent.mkdir()
            existing.write_bytes(b"%PDF-identical-content")
            (staging / filename).write_bytes(b"%PDF-identical-content")
            config.write_text(
                json.dumps(
                    {
                        "download_settings": {
                            "staging_dir": str(staging),
                            "archive_root": str(library_pdf_root),
                            "allowed_extensions": [".pdf"],
                        },
                        "research_library": {"root": str(library_root)},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = tmp_path / "run.json"
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT_PATH),
                    "--config", str(config), "--keywords", str(keywords), "--state", str(state),
                    "--window-start", "2026-07-14T08:00:00+08:00",
                    "--window-end", "2026-07-14T12:00:00+08:00",
                    "--downloaded-after", "2026-07-14T00:00:00+08:00",
                    "--run-id", "55555555-5555-4555-8555-555555555555",
                    "--scan-plan", str(plan), "--run-manifest", str(manifest),
                    "--commit-state",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            summary = json.loads(completed.stdout)
            saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            saved_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(summary["file_count"], 0)
            self.assertEqual(summary["satisfied_candidate_count"], 1)
            self.assertEqual(saved_manifest["downloaded_count"], 0)
            self.assertEqual(saved_manifest["satisfied_count"], 1)
            self.assertEqual(saved_manifest["missing_candidate_count"], 0)
            self.assertTrue(saved_manifest["state_commit_eligible"])
            self.assertEqual(
                saved_manifest["satisfied_entries"][0]["disposition"],
                "already_archived_content_duplicate",
            )
            self.assertEqual(saved_state["last_successful_check_at"], "2026-07-14T12:00:00+08:00")
            self.assertEqual(saved_state["last_batch_file_count"], 0)

    def test_same_filename_plan_rows_reconcile_to_one_physical_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            filename = "高盛-互联网行业深度报告.pdf"
            staging, _, config, keywords, state, plan = write_run_fixture(
                tmp_path,
                [filename, filename],
            )
            plan_payload = json.loads(plan.read_text(encoding="utf-8"))
            plan_payload["download_candidates"][0]["file_id"] = "newer-file"
            plan_payload["download_candidates"][0]["topic_id"] = "newer-topic"
            plan_payload["download_candidates"][1]["file_id"] = "older-file"
            plan_payload["download_candidates"][1]["topic_id"] = "older-topic"
            plan.write_text(
                json.dumps(plan_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            (staging / filename).write_bytes(b"%PDF-one-physical-file")
            manifest = tmp_path / "run.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--config",
                    str(config),
                    "--keywords",
                    str(keywords),
                    "--state",
                    str(state),
                    "--window-start",
                    "2026-07-14T08:00:00+08:00",
                    "--window-end",
                    "2026-07-14T12:00:00+08:00",
                    "--downloaded-after",
                    "2026-07-14T00:00:00+08:00",
                    "--run-id",
                    "66666666-6666-4666-8666-666666666666",
                    "--scan-plan",
                    str(plan),
                    "--run-manifest",
                    str(manifest),
                    "--commit-state",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            summary = json.loads(completed.stdout)
            saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            saved_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(summary["file_count"], 1)
            self.assertEqual(summary["satisfied_candidate_count"], 1)
            self.assertEqual(saved_manifest["downloaded_count"], 1)
            self.assertEqual(saved_manifest["satisfied_count"], 1)
            self.assertEqual(saved_manifest["missing_candidate_count"], 0)
            self.assertTrue(saved_manifest["state_commit_eligible"])
            self.assertEqual(
                saved_manifest["satisfied_entries"][0]["disposition"],
                "same_window_filename_duplicate",
            )
            self.assertEqual(
                saved_state["last_successful_check_at"],
                "2026-07-14T12:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()

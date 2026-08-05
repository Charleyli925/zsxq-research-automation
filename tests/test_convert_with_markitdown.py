from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "convert_with_markitdown.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("convert_with_markitdown", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ConvertWithMarkitdownTests(unittest.TestCase):
    def test_preflight_checks_pdf_conversion(self) -> None:
        result = MODULE.preflight()

        self.assertTrue(result["ok"], result)
        self.assertTrue(any(check["name"] == "markitdown_pdf_smoke" for check in result["checks"]))

    def test_process_batch_writes_raw_markdown_and_report_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            pdf_path = base / "alpha.pdf"
            batch_path = base / "batch.json"
            pdf_path.write_bytes(b"%PDF-test")
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "alpha.pdf",
                                "batch_id": "2026-05-07_10-00-00__to__2026-05-07_10-10-00",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(MODULE, "convert_pdf", return_value="# Alpha\n\nBody text"):
                result = MODULE.process_batch(batch_path, library_root)

            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]
            raw_path = Path(updated["raw_md_path"])

            self.assertEqual(result["raw_ready_count"], 1)
            self.assertTrue(raw_path.exists())
            self.assertEqual(raw_path.parent.name, "2026-05-07_10-00-00__to__2026-05-07_10-10-00")
            self.assertEqual(raw_path.name, "alpha.raw.md")
            self.assertEqual(raw_path.read_text(encoding="utf-8").strip(), "# Alpha\n\nBody text")
            self.assertTrue(updated["report_id"].startswith("zsxq_"))
            self.assertEqual(len(updated["pdf_sha256"]), 64)

    def test_event_write_failure_does_not_fail_raw_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            pdf_path = base / "alpha.pdf"
            batch_path = base / "batch.json"
            pdf_path.write_bytes(b"%PDF-test")
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "alpha.pdf",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(MODULE, "convert_pdf", return_value="# Alpha\n\nBody text"),
                patch.object(MODULE, "record_event", side_effect=RuntimeError("event failed")),
            ):
                result = MODULE.process_batch(batch_path, library_root)

            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]

            self.assertEqual(result["raw_ready_count"], 1)
            self.assertNotIn("raw_md_error", updated)
            self.assertTrue(Path(updated["raw_md_path"]).exists())

    def test_convert_pdf_timeout_does_not_retry_markitdown_cli(self) -> None:
        with (
            patch.object(MODULE, "run_python_markitdown", side_effect=MODULE.MarkItDownTimeoutError("timeout")),
            patch.object(MODULE.subprocess, "run") as subprocess_run,
        ):
            with self.assertRaises(MODULE.MarkItDownTimeoutError):
                MODULE.convert_pdf(Path("/tmp/slow.pdf"))

            subprocess_run.assert_not_called()

    def test_convert_pdf_rejects_empty_cli_output(self) -> None:
        completed = MODULE.subprocess.CompletedProcess(
            args=["markitdown", "/tmp/empty.pdf"],
            returncode=0,
            stdout="\n",
            stderr="",
        )
        with (
            patch.object(MODULE, "run_python_markitdown", return_value=""),
            patch.object(MODULE.subprocess, "run", return_value=completed),
        ):
            with self.assertRaisesRegex(RuntimeError, "returned empty output"):
                MODULE.convert_pdf(Path("/tmp/empty.pdf"))


if __name__ == "__main__":
    unittest.main()

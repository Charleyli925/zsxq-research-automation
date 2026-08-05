from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_clean_markdown.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("build_clean_markdown", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BuildCleanMarkdownTests(unittest.TestCase):
    def build_report_text(self, line_count: int) -> str:
        return "\n".join(
            f"Section {index:03d} has revenue growth, margin change, valuation risk, and market demand details."
            for index in range(line_count)
        )

    def test_usable_clean_markdown_becomes_extracted_text_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            raw_path = base / "alpha.raw.md"
            pdf_path = base / "alpha.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            raw_path.write_text(self.build_report_text(40), encoding="utf-8")
            batch_path = base / "batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "alpha.pdf",
                                "batch_id": "2026-05-07_10-00-00__to__2026-05-07_10-10-00",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                                "raw_md_path": str(raw_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = MODULE.process_batch(batch_path, library_root)
            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]

            self.assertEqual(result["clean_ready_count"], 1)
            self.assertEqual(updated["text_source"], "markitdown_clean")
            self.assertTrue(Path(updated["clean_md_path"]).exists())
            self.assertEqual(Path(updated["clean_md_path"]).parent.name, "2026-05-07_10-00-00__to__2026-05-07_10-10-00")
            self.assertEqual(Path(updated["clean_md_path"]).name, "alpha.clean.md")
            self.assertEqual(updated["extracted_text_path"], updated["clean_md_path"])

    def test_short_clean_markdown_leaves_existing_extractor_to_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            raw_path = base / "short.raw.md"
            pdf_path = base / "short.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            raw_path.write_text("too short\n", encoding="utf-8")
            batch_path = base / "batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "short.pdf",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                                "raw_md_path": str(raw_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = MODULE.process_batch(batch_path, library_root)
            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]

            self.assertEqual(result["clean_ready_count"], 0)
            self.assertIn("clean_md_warning", updated)
            self.assertNotIn("extracted_text_path", updated)
            self.assertTrue(Path(updated["clean_md_path"]).exists())

    def test_missing_raw_markdown_path_is_not_treated_as_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            pdf_path = base / "empty.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            with (
                patch.object(MODULE, "upsert_report") as upsert_report,
                patch.object(MODULE, "record_event") as record_event,
            ):
                updated = MODULE.process_item(
                    {
                        "path": str(pdf_path),
                        "filename": "empty.pdf",
                        "raw_md_error": "MarkItDown returned empty output",
                    },
                    library_root,
                )

            self.assertEqual(updated["clean_md_error"], "raw markdown missing")
            self.assertNotIn("Is a directory", updated["clean_md_error"])
            self.assertNotIn("raw_md_path", updated)
            self.assertEqual(upsert_report.call_args.args[1]["raw_md_path"], "")
            self.assertEqual(record_event.call_args.args[1]["artifact_path"], str(pdf_path))

    def test_long_english_line_is_wrapped_to_clean_limit(self) -> None:
        raw_text = " ".join(
            f"Section {index:03d} explains revenue growth margin pressure valuation risk demand recovery"
            for index in range(120)
        )

        clean_text = MODULE.clean_raw_markdown(raw_text)

        self.assertGreater(clean_text.count("\n"), 0)
        self.assertLessEqual(max(len(line) for line in clean_text.splitlines()), MODULE.MAX_CLEAN_LINE_CHARS)

    def test_long_chinese_line_without_spaces_is_wrapped_to_clean_limit(self) -> None:
        raw_text = "半导体需求恢复库存压力价格变化资本开支下游订单改善" * 180

        clean_text = MODULE.clean_raw_markdown(raw_text)

        self.assertGreater(clean_text.count("\n"), 0)
        self.assertLessEqual(max(len(line) for line in clean_text.splitlines()), MODULE.MAX_CLEAN_LINE_CHARS)

    def test_short_numbers_are_preserved_as_page_text(self) -> None:
        clean_text = MODULE.clean_raw_markdown("1\n\n# 标题\n\n2026\n\n正文段落保留数字。")

        self.assertIn("1", clean_text.splitlines())
        self.assertIn("2026", clean_text.splitlines())

    def test_inline_watermark_in_long_markitdown_line_keeps_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            raw_path = base / "single-line.raw.md"
            pdf_path = base / "single-line.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            body = " ".join(
                f"Section {index:03d} explains supply disruption, inventory pressure, oil price risk, and demand response."
                for index in range(40)
            )
            raw_path.write_text(
                body + " 知识星球：前沿信息收录VX:FCCNN88知识星球：前沿信息收录 微信：FCCNN88 " + body,
                encoding="utf-8",
            )
            batch_path = base / "batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "single-line.pdf",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                                "raw_md_path": str(raw_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = MODULE.process_batch(batch_path, library_root)
            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]
            clean_text = Path(updated["clean_md_path"]).read_text(encoding="utf-8")

            self.assertEqual(result["clean_ready_count"], 1)
            self.assertEqual(updated["text_source"], "markitdown_clean")
            self.assertIn("Section 000 explains supply disruption", clean_text)
            self.assertNotIn("FCCNN88", clean_text)
            self.assertLessEqual(max(len(line) for line in clean_text.splitlines()), MODULE.MAX_CLEAN_LINE_CHARS)

    def test_event_write_failure_does_not_fail_clean_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            raw_path = base / "alpha.raw.md"
            pdf_path = base / "alpha.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            raw_path.write_text(self.build_report_text(40), encoding="utf-8")
            batch_path = base / "batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(pdf_path),
                                "filename": "alpha.pdf",
                                "modified_at": "2026-05-07T10:00:00+08:00",
                                "raw_md_path": str(raw_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(MODULE, "record_event", side_effect=RuntimeError("event failed")):
                result = MODULE.process_batch(batch_path, library_root)

            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]

            self.assertEqual(result["clean_ready_count"], 1)
            self.assertNotIn("clean_md_error", updated)
            self.assertEqual(updated["text_source"], "markitdown_clean")
            self.assertTrue(Path(updated["clean_md_path"]).exists())


if __name__ == "__main__":
    unittest.main()

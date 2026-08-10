"""
这个文件测 `extract_pdf_text.py` 里最关键的两块：
- `ocrmypdf` 命令怎么拼
- 文本质量门禁会不会误杀正常研报

关系：
- `extract_pdf_text.py` 真正负责 PDF 正文提取。
- 这里不跑真实 OCR，只测最核心的判断规则。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "src" / "zsxq_pipeline" / "extractor_worker.py"
SPEC = importlib.util.spec_from_file_location("extract_pdf_text", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ExtractPdfTextTests(unittest.TestCase):
    def build_structured_report(self, line_count: int, weird_tokens: int) -> str:
        lines = [
            (
                f"Section {index:04d} discusses semiconductor demand, earnings revisions, "
                f"capital spending plans, and valuation changes across the market."
            )
            for index in range(line_count)
        ]
        for index in range(weird_tokens):
            lines.append(f"TOKEN{index:02d}ABCDEFGHIJKLMNOPQRSTUVWX")
        return "\n".join(lines)

    def test_build_ocrmypdf_command_uses_sidecar_only_mode_by_default(self) -> None:
        pdf_path = Path("/tmp/sample.pdf")
        sidecar_path = Path("/tmp/out.txt")

        with patch.object(MODULE, "TESSERACT_OCR_LANG", "eng"):
            command = MODULE.build_ocrmypdf_command(pdf_path, sidecar_path, output_pdf=None)

        self.assertEqual(
            command,
            [
                "ocrmypdf",
                "--force-ocr",
                "-l",
                "eng",
                "--sidecar",
                "/tmp/out.txt",
                "--output-type",
                "none",
                "/tmp/sample.pdf",
                "-",
            ],
        )

    def test_build_ocrmypdf_command_keeps_legacy_pdf_output_for_fallback(self) -> None:
        pdf_path = Path("/tmp/sample.pdf")
        sidecar_path = Path("/tmp/out.txt")
        output_pdf = Path("/tmp/ocr.pdf")

        with patch.object(MODULE, "TESSERACT_OCR_LANG", "eng+chi_sim"):
            command = MODULE.build_ocrmypdf_command(pdf_path, sidecar_path, output_pdf=output_pdf)

        self.assertEqual(
            command,
            [
                "ocrmypdf",
                "--force-ocr",
                "-l",
                "eng+chi_sim",
                "--sidecar",
                "/tmp/out.txt",
                "/tmp/sample.pdf",
                "/tmp/ocr.pdf",
            ],
        )

    def test_analyze_text_accepts_large_report_with_more_chart_tokens(self) -> None:
        text = self.build_structured_report(line_count=1100, weird_tokens=14)

        quality = MODULE.analyze_text(text)

        self.assertTrue(quality.acceptable)
        self.assertEqual(quality.weird_token_count, 14)
        self.assertGreaterEqual(MODULE.allowed_weird_token_count(quality.char_count, quality.meaningful_lines), 16)

    def test_analyze_text_accepts_large_ocr_with_proportional_noise(self) -> None:
        text = self.build_structured_report(line_count=340, weird_tokens=17)

        quality = MODULE.analyze_text(text)

        self.assertTrue(quality.acceptable)
        self.assertEqual(quality.weird_token_count, 17)

    def test_analyze_text_keeps_short_noisy_text_rejected(self) -> None:
        text = self.build_structured_report(line_count=20, weird_tokens=9)

        quality = MODULE.analyze_text(text)

        self.assertFalse(quality.acceptable)
        self.assertEqual(quality.weird_token_count, 9)
        self.assertIn("weird_limit=8", quality.summary)

    def test_analyze_text_still_rejects_watermark_dominated_text(self) -> None:
        text = "\n".join(["知识星球 前沿信息收录 VX FCCNN88"] * 200)

        quality = MODULE.analyze_text(text)

        self.assertFalse(quality.acceptable)
        self.assertGreaterEqual(quality.watermark_ratio, 0.9)

    def test_pdfinfo_geometry_parser_reads_variable_page_sizes(self) -> None:
        output = """Pages:           2
Page    1 size:  612 x 792 pts (letter)
Page    2 size:  1065 x 12631.5 pts
"""

        geometries = MODULE.parse_pdfinfo_geometries(output)

        self.assertEqual(
            geometries,
            [
                MODULE.PageGeometry(1, 612.0, 792.0),
                MODULE.PageGeometry(2, 1065.0, 12631.5),
            ],
        )

    def test_normal_page_plan_keeps_200_dpi(self) -> None:
        plan = MODULE.plan_page_ocr(MODULE.PageGeometry(1, 612.0, 792.0))

        self.assertEqual(plan.strategy, "standard")
        self.assertEqual(plan.dpi, 200)
        self.assertLess(plan.estimated_pixels, MODULE.OCR_MAX_PAGE_PIXELS)

    def test_meta_sized_page_plan_uses_adaptive_96_dpi(self) -> None:
        plan = MODULE.plan_page_ocr(MODULE.PageGeometry(1, 1065.0, 12631.5))

        self.assertEqual(plan.strategy, "adaptive")
        self.assertEqual(plan.dpi, 96)
        self.assertEqual((plan.width_px, plan.height_px), (1420, 16842))
        self.assertLessEqual(plan.estimated_pixels, MODULE.OCR_MAX_PAGE_PIXELS)

    def test_page_still_too_tall_at_minimum_dpi_uses_vertical_tiles(self) -> None:
        plan = MODULE.plan_page_ocr(MODULE.PageGeometry(1, 1065.0, 30000.0))

        self.assertEqual(plan.strategy, "tiled")
        self.assertEqual(plan.dpi, 96)
        self.assertGreater(plan.tile_count, 1)
        self.assertLessEqual(plan.tile_height * plan.width_px, MODULE.OCR_MAX_PAGE_PIXELS)
        self.assertLessEqual(plan.tile_count, MODULE.OCR_MAX_TILES_PER_PAGE)

    def test_unsupported_width_is_deterministic_geometry_failure(self) -> None:
        with self.assertRaisesRegex(MODULE.UnsupportedPageGeometryError, "unsupported_page_geometry"):
            MODULE.plan_page_ocr(MODULE.PageGeometry(1, 30000.0, 792.0))

        self.assertEqual(
            MODULE.classify_runtime_failure(["tesseract_failed: Image too large"]),
            ("content_failure", "unsupported_page_geometry", False),
        )

    def test_overlap_merge_removes_duplicate_boundary_lines(self) -> None:
        merged = MODULE.merge_overlapping_ocr_chunks(
            [
                "第一段正文说明行业需求变化。\n边界段落讨论库存和价格趋势。",
                "边界段落讨论库存和价格趋势。\n第二段正文说明资本开支变化。",
            ]
        )

        self.assertEqual(merged.count("边界段落讨论库存和价格趋势。"), 1)
        self.assertLess(merged.index("第一段"), merged.index("第二段"))

    def test_overlap_merge_keeps_similar_numbered_lines(self) -> None:
        merged = MODULE.merge_overlapping_ocr_chunks(
            [
                "Section 04 semiconductor demand inventory pricing capital spending outlook",
                "Section 05 semiconductor demand inventory pricing capital spending outlook",
            ]
        )

        self.assertIn("Section 04", merged)
        self.assertIn("Section 05", merged)

    def test_nonstandard_geometry_skips_ocrmypdf_and_uses_planned_fallback(self) -> None:
        geometry = MODULE.PageGeometry(1, 1065.0, 12631.5)
        diagnostics: dict[str, object] = {}
        with (
            patch.object(MODULE, "probe_pdf_geometry", return_value=[geometry]),
            patch.object(MODULE, "ocrmypdf_extract") as ocrmypdf,
            patch.object(
                MODULE,
                "pdftoppm_tesseract_extract",
                return_value=(self.build_structured_report(80, 0), "ocr_pdftoppm_tesseract_adaptive", False),
            ) as fallback,
        ):
            _, source, _, warnings = MODULE.local_ocr_extract(
                Path("/tmp/meta.pdf"),
                Path("/tmp/meta.txt"),
                diagnostics=diagnostics,
            )

        ocrmypdf.assert_not_called()
        self.assertEqual(source, "ocr_pdftoppm_tesseract_adaptive")
        self.assertIn("ocrmypdf_skipped_nonstandard_geometry", warnings)
        passed_plan = fallback.call_args.kwargs["plans"][0]
        self.assertEqual((passed_plan.strategy, passed_plan.dpi), ("adaptive", 96))
        self.assertEqual(diagnostics["strategy"], "adaptive")

    def test_runtime_image_limit_retries_as_tiles_in_same_call(self) -> None:
        initial_plan = MODULE.plan_document_ocr([MODULE.PageGeometry(1, 612.0, 792.0)])
        diagnostics: dict[str, object] = {}
        good_text = self.build_structured_report(80, 0)
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "result.txt"
            with (
                patch.object(MODULE.shutil, "which", return_value="/bin/fake"),
                patch.object(
                    MODULE,
                    "execute_pdftoppm_plan",
                    side_effect=[RuntimeError("tesseract_failed: Image too large"), [good_text]],
                ) as execute,
            ):
                text, source, truncated = MODULE.pdftoppm_tesseract_extract(
                    Path("/tmp/sample.pdf"),
                    output_path,
                    plans=initial_plan,
                    diagnostics=diagnostics,
                )

        self.assertFalse(truncated)
        self.assertIn("Section 0000", text)
        self.assertEqual(source, "ocr_pdftoppm_tesseract_tiled")
        self.assertEqual(execute.call_count, 2)
        retry_plan = execute.call_args_list[1].args[2][0]
        self.assertEqual(retry_plan.strategy, "tiled")
        self.assertEqual(diagnostics["initial_strategy"], "standard")

    def test_geometry_exhaustion_is_nonretryable_and_failure_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path = tmp_path / "too-wide.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nstub")
            item = {"path": str(pdf_path), "filename": pdf_path.name}
            cache_dir = tmp_path / "cache"
            geometry_error = MODULE.UnsupportedPageGeometryError(
                "unsupported_page_geometry: page=1 width exceeds limit"
            )
            with (
                patch.object(MODULE, "TEXT_CACHE_DIR", cache_dir),
                patch.object(MODULE, "direct_extract", return_value=("", "")),
                patch.object(MODULE, "local_ocr_extract", side_effect=geometry_error) as local_ocr,
            ):
                first = MODULE.ensure_text_for_item(dict(item), tmp_path / "out")
                second = MODULE.ensure_text_for_item(dict(item), tmp_path / "out")

        self.assertEqual(first["text_extract_error_type"], "content_failure")
        self.assertEqual(first["text_extract_error_code"], "unsupported_page_geometry")
        self.assertFalse(first["text_extract_retryable"])
        self.assertFalse(first["text_extract_cached"])
        self.assertTrue(second["text_extract_cached"])
        self.assertEqual(local_ocr.call_count, 1)

    def test_success_and_failure_cache_require_current_profile_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path = tmp_path / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nstub")
            with patch.object(MODULE, "TEXT_CACHE_DIR", tmp_path / "cache"):
                MODULE.save_success_cache(
                    "success-key",
                    pdf_path,
                    self.build_structured_report(80, 0),
                    "ocr_pdftoppm_tesseract",
                    [],
                    diagnostics={"strategy": "standard"},
                )
                self.assertIsNotNone(MODULE.load_success_cache("success-key"))
                success_meta, _ = MODULE.cache_paths("success-key")
                success_payload = json.loads(success_meta.read_text(encoding="utf-8"))
                success_payload["extractor_profile"] = "old-profile"
                success_meta.write_text(json.dumps(success_payload), encoding="utf-8")
                self.assertIsNone(MODULE.load_success_cache("success-key"))

                MODULE.save_failure_cache(
                    "failure-key",
                    pdf_path,
                    message="unsupported",
                    error_type="content_failure",
                    error_code="unsupported_page_geometry",
                    retryable=False,
                    warnings=[],
                    diagnostics={"strategy": "unsupported"},
                )
                self.assertIsNotNone(MODULE.load_failure_cache("failure-key"))
                failure_meta, _ = MODULE.cache_paths("failure-key")
                failure_payload = json.loads(failure_meta.read_text(encoding="utf-8"))
                failure_payload["cache_version"] = "old-version"
                failure_meta.write_text(json.dumps(failure_payload), encoding="utf-8")
                self.assertIsNone(MODULE.load_failure_cache("failure-key"))

    def test_ensure_text_for_item_uses_markitdown_clean_when_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path = tmp_path / "sample.pdf"
            clean_path = tmp_path / "sample.clean.md"
            pdf_path.write_bytes(b"%PDF-1.4\nstub")
            clean_path.write_text(self.build_structured_report(line_count=80, weird_tokens=0), encoding="utf-8")

            item = {
                "path": str(pdf_path),
                "filename": "sample.pdf",
                "extracted_text_path": str(clean_path),
                "text_source": "markitdown_clean",
                "text_extract_cache_key": "abc123",
            }

            updated = MODULE.ensure_text_for_item(item, tmp_path / "out")

            self.assertEqual(updated["extracted_text_path"], str(clean_path.resolve()))
            self.assertEqual(updated["text_source"], "markitdown_clean")
            self.assertEqual(updated["text_extract_cache_key"], "abc123")

    def test_ensure_text_falls_back_when_markitdown_clean_has_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path = tmp_path / "single-line.pdf"
            clean_path = tmp_path / "single-line.clean.md"
            pdf_path.write_bytes(b"%PDF-1.4\nstub")
            clean_path.write_text(
                " ".join(
                    f"Section {index:03d} explains supply disruption, inventory pressure, oil price risk, and demand response."
                    for index in range(80)
                ),
                encoding="utf-8",
            )

            item = {
                "path": str(pdf_path),
                "filename": "single-line.pdf",
                "extracted_text_path": str(clean_path),
                "text_source": "markitdown_clean",
                "text_extract_cache_key": "single-line-key",
            }

            fallback_text = self.build_structured_report(line_count=80, weird_tokens=0)
            with patch.object(MODULE, "direct_extract", return_value=(fallback_text, "")):
                updated = MODULE.ensure_text_for_item(item, tmp_path / "out")

            self.assertNotEqual(updated["extracted_text_path"], str(clean_path.resolve()))
            self.assertEqual(updated["text_source"], "pdftotext_fastpath")
            self.assertLessEqual(
                max(len(line) for line in Path(updated["extracted_text_path"]).read_text(encoding="utf-8").splitlines()),
                MODULE.MAX_TEXT_LINE_CHARS,
            )

    def test_sanitize_text_wraps_long_lines(self) -> None:
        text = " ".join(
            f"Section {index:03d} explains supply disruption and inventory pressure."
            for index in range(120)
        )

        cleaned, truncated = MODULE.sanitize_text(text, 20000)

        self.assertFalse(truncated)
        self.assertGreater(cleaned.count("\n"), 1)
        self.assertLess(max(len(line) for line in cleaned.splitlines()), 1300)

    def test_sanitize_text_wraps_long_chinese_lines_without_spaces(self) -> None:
        text = "半导体需求恢复库存压力价格变化资本开支下游订单改善" * 180

        cleaned, truncated = MODULE.sanitize_text(text, 20000)

        self.assertFalse(truncated)
        self.assertGreater(cleaned.count("\n"), 0)
        self.assertLessEqual(max(len(line) for line in cleaned.splitlines()), MODULE.MAX_TEXT_LINE_CHARS)

    def test_markitdown_clean_is_not_rejected_only_for_weird_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path = tmp_path / "weird-token.pdf"
            clean_path = tmp_path / "weird-token.clean.md"
            pdf_path.write_bytes(b"%PDF-1.4\nstub")
            clean_path.write_text(self.build_structured_report(line_count=80, weird_tokens=40), encoding="utf-8")

            item = {
                "path": str(pdf_path),
                "filename": "weird-token.pdf",
                "extracted_text_path": str(clean_path),
                "text_source": "markitdown_clean",
                "text_extract_cache_key": "weird-token-key",
            }

            updated = MODULE.ensure_text_for_item(item, tmp_path / "out")

            self.assertEqual(updated["text_source"], "markitdown_clean")
            self.assertEqual(updated["extracted_text_path"], str(clean_path.resolve()))


if __name__ == "__main__":
    unittest.main()

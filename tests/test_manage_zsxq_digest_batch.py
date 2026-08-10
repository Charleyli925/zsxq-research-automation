"""
这个文件只测一件事：飞书文档标题有没有按固定规则生成。

它测试的是 `scripts/manage_zsxq_digest_batch.py`。
这个脚本会把批次信息填进摘要 prompt，并提供 lark-cli 发布所需的小工具。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "manage_zsxq_digest_batch.py"
SPEC = importlib.util.spec_from_file_location("manage_zsxq_digest_batch", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManageZsxqDigestBatchTests(unittest.TestCase):
    def test_build_doc_title_uses_short_suffix_for_multi_doc_run(self) -> None:
        batch = {
            "generated_at": "2026-03-18T14:30:01.325533+08:00",
            "chunk_index": 2,
            "chunk_total": 4,
            "new_pdf_count": 10,
        }

        title = MODULE.build_doc_title(batch)

        self.assertEqual(title, "知识星球研报总结（2026-03-18） 10 篇 2/4")

    def test_build_doc_title_omits_suffix_for_single_doc_run(self) -> None:
        batch = {
            "generated_at": "2026-03-18T14:30:01.325533+08:00",
            "chunk_index": 1,
            "chunk_total": 1,
            "new_pdf_count": 3,
        }

        title = MODULE.build_doc_title(batch)

        self.assertEqual(title, "知识星球研报总结（2026-03-18） 3 篇")

    def test_render_prompt_includes_summary_manifest_and_system_prompt(self) -> None:
        batch = {
            "generated_at": "2026-03-18T14:30:01.325533+08:00",
            "chunk_index": 1,
            "chunk_total": 1,
            "new_pdf_count": 2,
            "total_pdf_count": 2,
            "files": [
                {
                    "filename": "a.pdf",
                    "path": "/tmp/a.pdf",
                    "extracted_text_path": "/tmp/a.txt",
                    "text_source": "pdftotext",
                    "extracted_text_chars": 123,
                    "title": "阿尔法研报",
                    "report_id": "report-a",
                }
            ],
        }

        template = (
            "{{EDITOR_SYSTEM_PROMPT}}\n"
            "批次={{CHUNK_INDEX}}/{{CHUNK_TOTAL}}\n"
            "文件={{CURRENT_FILE_MANIFEST}}\n"
            "清单={{CURRENT_PATH_JSON}}\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            template_path = tmp_path / "template.md"
            batch_path = tmp_path / "batch.json"
            system_prompt_path = tmp_path / "system.md"
            output_path = tmp_path / "output.md"

            template_path.write_text(template, encoding="utf-8")
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            system_prompt_path.write_text("摘要规则", encoding="utf-8")

            MODULE.render_prompt(
                template_path=template_path,
                batch_file=batch_path,
                system_prompt_file=system_prompt_path,
                output_path=output_path,
            )

            rendered = output_path.read_text(encoding="utf-8")

        self.assertIn("摘要规则", rendered)
        self.assertIn("批次=1/1", rendered)
        self.assertIn("1. a.pdf", rendered)
        self.assertIn("标题：阿尔法研报", rendered)
        self.assertIn("report_id：report-a", rendered)
        self.assertIn('清单=["/tmp/a.pdf"]', rendered)

    def test_check_batch_text_ready_returns_structured_failure_details(self) -> None:
        batch = {
            "files": [
                {
                    "filename": "broken.pdf",
                    "path": "/tmp/broken.pdf",
                    "text_extract_error": "文本抽取失败：系统环境异常，OCR fallback 未能正常运行",
                    "text_extract_error_type": "env_failure",
                    "text_extract_error_code": "ocr_tempfile_failure",
                    "text_extract_retryable": False,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_path = Path(tmp_dir) / "batch.json"
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            stream = io.StringIO()
            with redirect_stdout(stream):
                result_code = MODULE.check_batch_text_ready(batch_path)
            payload = json.loads(stream.getvalue())

        self.assertEqual(result_code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_types"], ["env_failure"])
        self.assertEqual(payload["retryable_failure_count"], 0)
        self.assertTrue(payload["has_env_failure"])
        self.assertFalse(payload["all_nonretryable_content_failures"])
        self.assertEqual(payload["failure_details"][0]["error_code"], "ocr_tempfile_failure")

    def test_check_batch_text_ready_blocks_overlong_clean_markdown_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            text_path = base / "sample.clean.md"
            text_path.write_text("A" * (MODULE.MAX_SUMMARY_INPUT_LINE_CHARS + 1), encoding="utf-8")
            batch = {
                "files": [
                    {
                        "filename": "sample.pdf",
                        "path": "/tmp/sample.pdf",
                        "extracted_text_path": str(text_path),
                        "extracted_text_chars": MODULE.MAX_SUMMARY_INPUT_LINE_CHARS + 1,
                        "text_source": "markitdown_clean",
                    }
                ]
            }
            batch_path = base / "batch.json"
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            stream = io.StringIO()

            with redirect_stdout(stream):
                result_code = MODULE.check_batch_text_ready(batch_path)
            payload = json.loads(stream.getvalue())

        self.assertEqual(result_code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("clean.md 格式不合格", payload["message"])
        self.assertEqual(payload["failure_details"][0]["error_code"], "summary_input_line_too_long")
        self.assertEqual(payload["failure_details"][0]["max_line_chars"], MODULE.MAX_SUMMARY_INPUT_LINE_CHARS + 1)
        self.assertTrue(payload["has_env_failure"])

    def test_update_quarantine_builds_readable_entries(self) -> None:
        batch = {
            "files": [
                {
                    "filename": "scan.pdf",
                    "path": "/tmp/scan.pdf",
                    "relative_path": "2026/scan.pdf",
                    "text_extract_error": "文本抽取失败：pdftotext 低质量，OCR 未拿到可用正文",
                    "text_extract_error_type": "content_failure",
                    "text_extract_error_code": "no_usable_text",
                    "text_extract_warning": "ocr_output_low_quality",
                    "text_extract_retryable": False,
                    "text_extract_cache_key": "abc123",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            quarantine_path = base / "quarantine.json"
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")

            stream = io.StringIO()
            with redirect_stdout(stream):
                result_code = MODULE.update_quarantine(batch_path, quarantine_path, "2026-03-28T10:00:00+08:00")
            payload = json.loads(quarantine_path.read_text(encoding="utf-8"))

        self.assertEqual(result_code, 0)
        self.assertTrue(json.loads(stream.getvalue())["updated"])
        self.assertEqual(payload["summary"]["total_entries"], 1)
        self.assertEqual(payload["summary"]["by_error_code"], {"no_usable_text": 1})
        self.assertEqual(payload["summary"]["by_next_step"], {"人工检查正文质量": 1})
        self.assertEqual(payload["entries"][0]["filename"], "scan.pdf")
        self.assertEqual(payload["entries"][0]["latest_error"], "文本抽取失败：pdftotext 低质量，OCR 未拿到可用正文")
        self.assertIn("人工检查正文", payload["entries"][0]["recommended_action"])
        self.assertIn("--dry-run --file /tmp/scan.pdf", payload["entries"][0]["suggested_command"])

    def test_inspect_quarantine_renders_manual_checklist(self) -> None:
        payload = {
            "items": {
                "/tmp/broken.pdf": {
                    "path": "/tmp/broken.pdf",
                    "filename": "broken.pdf",
                    "error_type": "content_failure",
                    "error_code": "pdf_parse_failure",
                    "latest_error": "文本抽取失败：PDF 内容结构异常，OCR fallback 未恢复出可读正文",
                    "failure_count": 2,
                    "first_quarantined_at": "2026-03-28T09:00:00+08:00",
                    "last_quarantined_at": "2026-03-28T10:30:00+08:00",
                }
            },
            "updated_at": "2026-03-28T10:30:00+08:00",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            quarantine_path = base / "quarantine.json"
            report_path = base / "quarantine_report.md"
            quarantine_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            stream = io.StringIO()
            with redirect_stdout(stream):
                result_code = MODULE.inspect_quarantine(quarantine_path, report_path)
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(result_code, 0)
        self.assertIn("ZSXQ PDF 隔离清单", report_text)
        self.assertIn("broken.pdf", report_text)
        self.assertIn("确认文件是否损坏", report_text)
        self.assertIn("bash <digest-task-dir>/run.sh --dry-run", report_text)
        self.assertIn("broken.pdf", stream.getvalue())

    def test_summary_artifacts_can_be_persisted_and_reloaded_from_cache(self) -> None:
        batch = {
            "chunk_index": 1,
            "chunk_total": 3,
            "files": [
                {
                    "filename": "alpha.pdf",
                    "path": "/tmp/alpha.pdf",
                    "extracted_text_path": "/tmp/alpha.txt",
                    "extracted_text_chars": 1234,
                    "text_source": "ocr_ocrmypdf_sidecar",
                    "text_extract_warning": "direct_probe_low_quality",
                    "text_extract_cache_key": "cache-alpha",
                }
            ],
        }
        summary_payload = {
            "status": "success",
            "handled_count": 1,
            "handled_paths": ["/tmp/alpha.pdf"],
            "summaries": [
                {
                    "path": "/tmp/alpha.pdf",
                    "filename": "alpha.pdf",
                    "title": "阿尔法研报",
                    "quality_hint": "原始文本可能不完整，以下结论可能受影响。",
                    "markdown": "# 阿尔法研报\n> 原始文件名：alpha.pdf\n\n## 核心结论\n- 结论一",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            result_path = base / "summary.result.md"
            summary_cache_dir = base / "summary_cache"
            output_json = base / "chunk.summary.json"
            output_markdown = base / "chunk.summary.md"
            materialized_json = base / "materialized.summary.json"
            materialized_markdown = base / "materialized.summary.md"

            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            result_path.write_text(
                "完成\nZSXQ_SUMMARY_JSON: " + json.dumps(summary_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            validate_stream = io.StringIO()
            with redirect_stdout(validate_stream):
                validate_code = MODULE.validate_summary_result(batch_path, result_path)
            validate_payload = json.loads(validate_stream.getvalue())

            self.assertEqual(validate_code, 0)
            self.assertEqual(validate_payload["summary_count"], 1)
            self.assertEqual(validate_payload["summaries"][0]["title"], "阿尔法研报")

            persist_stream = io.StringIO()
            with redirect_stdout(persist_stream):
                persist_code = MODULE.persist_summary_artifacts(
                    batch_file=batch_path,
                    result_file=result_path,
                    summary_cache_dir=summary_cache_dir,
                    output_json=output_json,
                    output_markdown=output_markdown,
                )
            persist_payload = json.loads(persist_stream.getvalue())

            self.assertEqual(persist_code, 0)
            self.assertTrue(persist_payload["ok"])
            self.assertTrue(output_json.exists())
            self.assertTrue(output_markdown.exists())
            self.assertIn("阿尔法研报", output_markdown.read_text(encoding="utf-8"))

            materialize_stream = io.StringIO()
            with redirect_stdout(materialize_stream):
                materialize_code = MODULE.materialize_summary_cache(
                    batch_file=batch_path,
                    summary_cache_dir=summary_cache_dir,
                    output_json=materialized_json,
                    output_markdown=materialized_markdown,
                )
            materialize_payload = json.loads(materialize_stream.getvalue())

            self.assertEqual(materialize_code, 0)
            self.assertTrue(materialize_payload["ok"])
            self.assertTrue(materialized_json.exists())
            self.assertTrue(materialized_markdown.exists())
            self.assertEqual(
                materialized_markdown.read_text(encoding="utf-8").strip(),
                output_markdown.read_text(encoding="utf-8").strip(),
            )

    def test_validate_handled_paths_tolerates_collapsed_internal_spaces(self) -> None:
        expected_paths = [
            "/tmp/260402-GS-高盛亚洲  定罪名单变更，中国互联网.pdf",
        ]
        handled_paths = [
            "/tmp/260402-GS-高盛亚洲 定罪名单变更，中国互联网.pdf",
        ]

        validated = MODULE._validate_handled_paths(expected_paths, handled_paths)

        self.assertEqual(validated, handled_paths)

    def test_persist_summary_writes_permanent_summary_when_library_root_is_set(self) -> None:
        batch = {
            "generated_at": "2026-05-07T10:00:00+08:00",
            "files": [
                {
                    "path": "/tmp/alpha.pdf",
                    "filename": "alpha.pdf",
                    "modified_at": "2026-05-07T10:00:00+08:00",
                    "text_extract_cache_key": "b" * 64,
                    "extracted_text_path": "/tmp/alpha.txt",
                    "extracted_text_chars": 1200,
                    "text_source": "markitdown_clean",
                }
            ],
        }
        result_text = (
            'ZSXQ_SUMMARY_JSON: {"status":"success","handled_count":1,'
            '"handled_paths":["/tmp/alpha.pdf"],'
            '"summaries":[{"path":"/tmp/alpha.pdf","filename":"alpha.pdf",'
            '"title":"阿尔法研报","quality_hint":"","markdown":"# 阿尔法研报\\n\\n## 核心结论\\n- A"}]}'
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            result_path = base / "result.md"
            output_json = base / "summary.json"
            output_markdown = base / "summary.md"
            summary_cache_dir = base / "summary_cache"
            library_root = base / "ResearchLibrary"
            batch_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            result_path.write_text(result_text, encoding="utf-8")

            with patch.dict(os.environ, {"RESEARCH_LIBRARY_ROOT": str(library_root)}):
                MODULE.persist_summary_artifacts(
                    batch_file=batch_path,
                    result_file=result_path,
                    summary_cache_dir=summary_cache_dir,
                    output_json=output_json,
                    output_markdown=output_markdown,
                )

            updated_batch = json.loads(batch_path.read_text(encoding="utf-8"))
            summary_md_path = Path(updated_batch["files"][0]["summary_md_path"])
            self.assertEqual(summary_md_path.name, "alpha.summary.md")
            self.assertTrue(summary_md_path.exists())

    def test_build_publish_groups_keeps_threshold_batch_in_one_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            summary_dir = base / "summaries"
            summary_dir.mkdir()
            files = []
            for index in range(15):
                summary_path = summary_dir / f"report-{index + 1}.summary.md"
                summary_path.write_text(f"# Report {index + 1}\n", encoding="utf-8")
                files.append(
                    {
                        "path": f"/tmp/report-{index + 1}.pdf",
                        "filename": f"report-{index + 1}.pdf",
                        "summary_md_path": str(summary_path),
                    }
                )
            chunk_path = base / "chunk-001.json"
            chunk_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-07T10:00:00+08:00",
                        "chunk_index": 1,
                        "chunk_total": 15,
                        "new_pdf_count": 15,
                        "files": files,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_dir = base / "publish"
            stream = io.StringIO()

            with redirect_stdout(stream):
                result_code = MODULE.build_publish_groups(
                    chunk_files=[chunk_path],
                    output_dir=output_dir,
                    doc_group_size=10,
                    doc_group_threshold=15,
                    total_file_count=15,
                )
            payload = json.loads(stream.getvalue())

            self.assertEqual(result_code, 0)
            self.assertEqual(payload["group_count"], 1)
            group_batch = json.loads(Path(payload["groups"][0]["batch_file"]).read_text(encoding="utf-8"))
            self.assertEqual(group_batch["new_pdf_count"], 15)
            self.assertEqual(group_batch["chunk_total"], 1)

    def test_build_publish_groups_splits_above_threshold_by_file_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            summary_dir = base / "summaries"
            summary_dir.mkdir()
            chunk_paths = []
            all_files = []
            for index in range(16):
                summary_path = summary_dir / f"report-{index + 1}.summary.md"
                summary_path.write_text(f"# Report {index + 1}\n", encoding="utf-8")
                all_files.append(
                    {
                        "path": f"/tmp/report-{index + 1}.pdf",
                        "filename": f"report-{index + 1}.pdf",
                        "summary_md_path": str(summary_path),
                    }
                )
            for chunk_index, start in enumerate((0, 7, 14), start=1):
                chunk_path = base / f"chunk-{chunk_index:03d}.json"
                chunk_path.write_text(
                    json.dumps(
                        {
                            "generated_at": "2026-05-07T10:00:00+08:00",
                            "chunk_index": chunk_index,
                            "chunk_total": 3,
                            "new_pdf_count": len(all_files[start : start + 7]),
                            "files": all_files[start : start + 7],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                chunk_paths.append(chunk_path)
            output_dir = base / "publish"
            stream = io.StringIO()

            with redirect_stdout(stream):
                result_code = MODULE.build_publish_groups(
                    chunk_files=chunk_paths,
                    output_dir=output_dir,
                    doc_group_size=10,
                    doc_group_threshold=15,
                    total_file_count=16,
                )
            payload = json.loads(stream.getvalue())

            self.assertEqual(result_code, 0)
            self.assertEqual(payload["group_count"], 2)
            self.assertEqual([group["file_count"] for group in payload["groups"]], [10, 6])
            first_batch = json.loads(Path(payload["groups"][0]["batch_file"]).read_text(encoding="utf-8"))
            second_batch = json.loads(Path(payload["groups"][1]["batch_file"]).read_text(encoding="utf-8"))
            self.assertEqual(len(first_batch["files"]), 10)
            self.assertEqual(len(second_batch["files"]), 6)
            first_markdown = Path(payload["groups"][0]["summary_markdown"]).read_text(encoding="utf-8")
            second_markdown = Path(payload["groups"][1]["summary_markdown"]).read_text(encoding="utf-8")
            self.assertIn("# Report 10", first_markdown)
            self.assertNotIn("# Report 11", first_markdown)
            self.assertIn("# Report 11", second_markdown)

    def test_build_publish_groups_can_use_global_group_numbers_for_incremental_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            summary_dir = base / "summaries"
            summary_dir.mkdir()
            files = []
            for index in range(10):
                summary_path = summary_dir / f"report-{index + 1}.summary.md"
                summary_path.write_text(f"# Report {index + 1}\n", encoding="utf-8")
                files.append(
                    {
                        "path": f"/tmp/report-{index + 1}.pdf",
                        "filename": f"report-{index + 1}.pdf",
                        "summary_md_path": str(summary_path),
                    }
                )
            chunk_path = base / "chunk-001.json"
            chunk_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-24T12:50:01+08:00",
                        "chunk_index": 1,
                        "chunk_total": 88,
                        "new_pdf_count": 1,
                        "files": files,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_dir = base / "publish"
            stream = io.StringIO()

            with redirect_stdout(stream):
                result_code = MODULE.build_publish_groups(
                    chunk_files=[chunk_path],
                    output_dir=output_dir,
                    doc_group_size=10,
                    doc_group_threshold=15,
                    total_file_count=88,
                    group_start_index=3,
                    group_total_override=9,
                )
            payload = json.loads(stream.getvalue())

            self.assertEqual(result_code, 0)
            self.assertEqual(payload["group_count"], 1)
            self.assertEqual(payload["groups"][0]["group_index"], 3)
            self.assertEqual(payload["groups"][0]["group_total"], 9)
            group_batch = json.loads(Path(payload["groups"][0]["batch_file"]).read_text(encoding="utf-8"))
            self.assertEqual(group_batch["chunk_index"], 3)
            self.assertEqual(group_batch["chunk_total"], 9)
            self.assertEqual(MODULE.build_doc_title(group_batch), "知识星球研报总结（2026-06-24） 10 篇 3/9")

    def test_build_lark_cli_create_markdown_keeps_doc_title_out_of_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "publish-group-001.batch.json"
            summary_path = base / "publish-group-001.summary.md"
            output_path = base / "create.md"
            batch_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-06T10:00:00+08:00",
                        "chunk_index": 1,
                        "chunk_total": 2,
                        "new_pdf_count": 10,
                        "files": [{"path": "/tmp/a.pdf", "filename": "a.pdf"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary_path.write_text("# A\n\n## 核心结论\n- 结论", encoding="utf-8")

            stream = io.StringIO()
            with redirect_stdout(stream):
                result_code = MODULE.build_lark_cli_create_markdown(batch_path, summary_path, output_path)

            self.assertEqual(result_code, 0)
            self.assertEqual(stream.getvalue().strip(), str(output_path))
            content = output_path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("# A\n\n## 核心结论"))
            self.assertNotIn("知识星球研报总结（2026-06-06） 10 篇 1/2", content)

    def test_publish_key_changes_when_target_doc_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "publish-group-001.batch.json"
            summary_path = base / "publish-group-001.summary.md"
            batch_path.write_text(
                json.dumps(
                    {
                        "new_pdf_count": 1,
                        "files": [
                            {
                                "path": "/tmp/a.pdf",
                                "filename": "a.pdf",
                                "summary_md_path": "/tmp/a.summary.md",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary_path.write_text("# A\n\n- 结论", encoding="utf-8")

            first = MODULE.build_publish_key_payload(batch_path, summary_path, "")
            second = MODULE.build_publish_key_payload(batch_path, summary_path, "https://www.feishu.cn/docx/demo")

            self.assertNotEqual(first["publish_key"], second["publish_key"])
            self.assertEqual(first["summary_hash"], second["summary_hash"])

    def test_publish_record_lookup_returns_latest_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            summary_path = base / "summary.md"
            records_path = base / "publish_records.jsonl"
            batch_path.write_text(
                json.dumps({"files": [{"path": "/tmp/a.pdf", "filename": "a.pdf"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            summary_path.write_text("# A", encoding="utf-8")
            key_payload = MODULE.build_publish_key_payload(batch_path, summary_path, "")

            MODULE.append_publish_record(
                records_file=records_path,
                publish_key=key_payload["publish_key"],
                batch_file=batch_path,
                summary_markdown=summary_path,
                target_doc_url="",
                doc_url="https://www.feishu.cn/docx/demo",
                mode="create",
                publisher="lark-cli",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.lookup_publish_record(records_path, key_payload["publish_key"])
            payload = json.loads(stream.getvalue())

            self.assertTrue(payload["found"])
            self.assertEqual(payload["doc_url"], "https://www.feishu.cn/docx/demo")

    def test_publish_ledger_records_inventory_and_recovers_remote_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            summary_path = base / "summary.md"
            records_path = base / "publish_records.jsonl"
            batch_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-14T12:00:00+08:00",
                        "new_pdf_count": 2,
                        "files": [
                            {
                                "path": "/tmp/a.pdf",
                                "filename": "a.pdf",
                                "report_id": "a-report",
                                "pdf_sha256": "a-sha",
                            },
                            {
                                "path": "/tmp/b.pdf",
                                "filename": "b.pdf",
                                "report_id": "b-report",
                                "text_extract_cache_key": "b-cache",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary_path.write_text("# A\n\n## 核心结论\n这是足够长的摘要锚点。", encoding="utf-8")
            key_payload = MODULE.build_publish_key_payload(batch_path, summary_path, "")

            with redirect_stdout(io.StringIO()):
                MODULE.append_publish_record(
                    records_file=records_path,
                    publish_key=key_payload["publish_key"],
                    batch_file=batch_path,
                    summary_markdown=summary_path,
                    target_doc_url="",
                    doc_url="",
                    mode="create",
                    publisher="lark-cli",
                    status="intent",
                )
                MODULE.append_publish_record(
                    records_file=records_path,
                    publish_key=key_payload["publish_key"],
                    batch_file=batch_path,
                    summary_markdown=summary_path,
                    target_doc_url="",
                    doc_url="https://www.feishu.cn/docx/recovery-demo",
                    mode="create",
                    publisher="lark-cli",
                    status="remote_written",
                )

            records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["status"] for record in records], ["intent", "remote_written"])
            self.assertEqual(records[-1]["report_date"], "2026-07-14")
            self.assertEqual(records[-1]["file_count"], 2)
            self.assertEqual([item["filename"] for item in records[-1]["files"]], ["a.pdf", "b.pdf"])
            self.assertEqual(records[-1]["files"][1]["pdf_sha256"], "b-cache")

            recovery_stream = io.StringIO()
            with redirect_stdout(recovery_stream):
                MODULE.lookup_publish_recovery(
                    records_path,
                    key_payload["batch_hash"],
                    key_payload["summary_hash"],
                )
            recovery = json.loads(recovery_stream.getvalue())
            self.assertTrue(recovery["found"])
            self.assertEqual(recovery["status"], "remote_written")
            self.assertEqual(recovery["doc_url"], "https://www.feishu.cn/docx/recovery-demo")

    def test_publish_recovery_matches_remote_write_after_group_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            first_batch_path = base / "first-batch.json"
            retry_batch_path = base / "retry-batch.json"
            summary_path = base / "summary.md"
            records_path = base / "publish_records.jsonl"
            files = [
                {"path": "/tmp/a.pdf", "filename": "a.pdf", "report_id": "a", "pdf_sha256": "a-sha"},
                {"path": "/tmp/b.pdf", "filename": "b.pdf", "report_id": "b", "pdf_sha256": "b-sha"},
            ]
            first_batch_path.write_text(
                json.dumps({"new_pdf_count": 213, "chunk_index": 2, "chunk_total": 22, "files": files}),
                encoding="utf-8",
            )
            retry_batch_path.write_text(
                json.dumps({"new_pdf_count": 203, "chunk_index": 1, "chunk_total": 21, "files": files}),
                encoding="utf-8",
            )
            summary_path.write_text("# A\n\n## 核心结论\n稳定的摘要正文锚点。", encoding="utf-8")
            first = MODULE.build_publish_key_payload(first_batch_path, summary_path, "")
            retry = MODULE.build_publish_key_payload(retry_batch_path, summary_path, "")
            self.assertNotEqual(first["batch_hash"], retry["batch_hash"])

            with redirect_stdout(io.StringIO()):
                MODULE.append_publish_record(
                    records_file=records_path,
                    publish_key=first["publish_key"],
                    batch_file=first_batch_path,
                    summary_markdown=summary_path,
                    target_doc_url="",
                    doc_url="https://www.feishu.cn/docx/recovery-after-rebatch",
                    mode="create",
                    publisher="lark-cli",
                    status="remote_written",
                )

            recovery_stream = io.StringIO()
            with redirect_stdout(recovery_stream):
                MODULE.lookup_publish_recovery(
                    records_path,
                    retry["batch_hash"],
                    retry["summary_hash"],
                    retry_batch_path,
                )
            recovery = json.loads(recovery_stream.getvalue())
            self.assertTrue(recovery["found"])
            self.assertEqual(recovery["recovery_match"], "file_identity")
            self.assertEqual(recovery["doc_url"], "https://www.feishu.cn/docx/recovery-after-rebatch")

    def test_same_day_lookup_uses_conservative_legacy_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            records_path = base / "publish_records.jsonl"
            doc_url = "https://www.feishu.cn/docx/J3JcdqF44oqMJCxr3lwcUXRXnzb"
            batch_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-14T12:00:00+08:00",
                        "new_pdf_count": 1,
                        "files": [{"path": "/tmp/meta.pdf", "filename": "Meta.pdf"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            records_path.write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-14T08:49:00+08:00",
                        "status": "success",
                        "publish_key": "legacy-key",
                        "doc_url": doc_url,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.lookup_latest_same_day_doc(
                    records_path,
                    batch_path,
                    incoming_file_count=1,
                    max_file_count=20,
                    legacy_file_count=10,
                )
            payload = json.loads(stream.getvalue())

            self.assertTrue(payload["found"])
            self.assertEqual(payload["doc_url"], doc_url)
            self.assertEqual(payload["current_file_count"], 10)
            self.assertEqual(payload["remaining_after"], 9)
            self.assertEqual(payload["legacy_estimated_record_count"], 1)

    def test_same_day_lookup_does_not_fall_back_when_latest_doc_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            records_path = base / "publish_records.jsonl"
            batch_path.write_text(
                json.dumps(
                    {
                        "report_date": "2026-07-14",
                        "new_pdf_count": 1,
                        "files": [{"path": "/tmp/new.pdf", "filename": "new.pdf"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            records = [
                {
                    "created_at": "2026-07-14T08:00:00+08:00",
                    "status": "success",
                    "publish_key": "older",
                    "doc_url": "https://www.feishu.cn/docx/older",
                    "report_date": "2026-07-14",
                    "file_count": 5,
                },
                {
                    "created_at": "2026-07-14T11:00:00+08:00",
                    "status": "success",
                    "publish_key": "latest",
                    "doc_url": "https://www.feishu.cn/docx/latest",
                    "report_date": "2026-07-14",
                    "file_count": 20,
                },
            ]
            records_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.lookup_latest_same_day_doc(
                    records_path,
                    batch_path,
                    incoming_file_count=1,
                    max_file_count=20,
                    legacy_file_count=10,
                )
            payload = json.loads(stream.getvalue())

            self.assertFalse(payload["found"])
            self.assertEqual(payload["reason"], "latest_same_day_doc_full")
            self.assertEqual(payload["latest_doc_url"], "https://www.feishu.cn/docx/latest")

    def test_parse_lark_cli_doc_url_accepts_token_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            output_path = base / "out.json"
            output_path.write_text(
                json.dumps({"data": {"document_id": "XDgSdLlXkoI6RPx2Lx1cZzPqnuh"}}, ensure_ascii=False),
                encoding="utf-8",
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.parse_lark_cli_doc_url(output_path, None, "", "https://www.feishu.cn/docx")
            payload = json.loads(stream.getvalue())

            self.assertEqual(payload["doc_url"], "https://www.feishu.cn/docx/XDgSdLlXkoI6RPx2Lx1cZzPqnuh")

    def test_nonretryable_content_failure_is_needs_transform_and_does_not_block_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            bad_pdf = base / "bad.pdf"
            good_pdf = base / "new.pdf"
            bad_pdf.write_bytes(b"bad-pdf")
            good_pdf.write_bytes(b"new-pdf")
            failed_batch = base / "failed.json"
            pending_batch = base / "pending.json"
            eligible_batch = base / "eligible.json"
            ledger_path = base / "stage_retry_ledger.json"
            failed_batch.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": str(bad_pdf),
                                "filename": bad_pdf.name,
                                "text_extract_error": "unsupported page geometry",
                                "text_extract_error_type": "content_failure",
                                "text_extract_error_code": "unsupported_page_geometry",
                                "text_extract_retryable": False,
                                "text_extract_profile": "ocr-geometry-v2",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                MODULE.record_stage_retry(
                    failed_batch,
                    ledger_path,
                    stage="text_extract",
                    run_at="2026-07-14T08:50:00+08:00",
                    workflow_version="workflow-v2",
                )

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            entry = next(iter(ledger["entries"].values()))
            self.assertEqual(entry["status"], "needs_transform")
            self.assertEqual(entry["failure_count"], 1)
            self.assertIsNone(entry["next_retry_at"])
            self.assertEqual(entry["extractor_profile"], "ocr-geometry-v2")

            pending_batch.write_text(
                json.dumps(
                    {
                        "new_pdf_count": 2,
                        "files": [
                            {"path": str(bad_pdf), "filename": bad_pdf.name},
                            {"path": str(good_pdf), "filename": good_pdf.name},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.filter_stage_retries(
                    pending_batch,
                    ledger_path,
                    eligible_batch,
                    stage="any",
                    run_at="2026-07-14T10:30:00+08:00",
                    workflow_version="workflow-v2",
                )

            status = json.loads(stream.getvalue())
            eligible = json.loads(eligible_batch.read_text(encoding="utf-8"))
            self.assertEqual(status["deferred_count"], 1)
            self.assertEqual([item["filename"] for item in eligible["files"]], ["new.pdf"])
            self.assertEqual(next(iter(json.loads(ledger_path.read_text())["entries"].values()))["failure_count"], 1)

    def test_contract_version_describes_publish_recovery_argument_boundary(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = MODULE.contract_version()

        self.assertEqual(result, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["contract_version"], "zsxq-digest-batch/v1")
        self.assertIn(
            "--batch-file",
            payload["commands"]["lookup-publish-recovery"]["arguments"],
        )

    def test_release_contract_mismatch_is_blocked_without_next_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            pdf = base / "blocked.pdf"
            pdf.write_bytes(b"blocked")
            batch_path = base / "batch.json"
            ledger_path = base / "ledger.json"
            eligible_path = base / "eligible.json"
            batch_path.write_text(
                json.dumps({"files": [{"path": str(pdf), "filename": pdf.name}]}),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                MODULE.record_stage_retry(
                    batch_path,
                    ledger_path,
                    stage="publish",
                    run_at="2026-08-10T12:00:00+08:00",
                    workflow_version="legacy-release",
                    error_code_override="release_contract_mismatch",
                    error_type_override="release_contract_mismatch",
                    retryable_override="false",
                    message_override="unrecognized arguments: --batch-file",
                )

            entry = next(iter(json.loads(ledger_path.read_text(encoding="utf-8"))["entries"].values()))
            self.assertEqual(entry["status"], "blocked_release")
            self.assertFalse(entry["retryable"])
            self.assertIsNone(entry["next_retry_at"])

            stream = io.StringIO()
            with redirect_stdout(stream):
                MODULE.filter_stage_retries(
                    batch_path,
                    ledger_path,
                    eligible_path,
                    stage="any",
                    run_at="2026-08-10T12:10:00+08:00",
                    workflow_version="legacy-release",
                )
            status = json.loads(stream.getvalue())
            self.assertEqual(status["eligible_count"], 0)
            self.assertTrue(status["all_deferred_terminal_blocked"])
            self.assertEqual(status["terminal_blocked_count"], 1)

    def test_recover_stage_retries_is_preview_first_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            batch_path = base / "batch.json"
            ledger_path = base / "ledger.json"
            files = []
            for name in ("legacy-a.pdf", "legacy-b.pdf"):
                pdf = base / name
                pdf.write_bytes(name.encode("utf-8"))
                files.append({"path": str(pdf), "filename": name})
            batch_path.write_text(json.dumps({"files": files}), encoding="utf-8")

            for attempt in range(4):
                with redirect_stdout(io.StringIO()):
                    MODULE.record_stage_retry(
                        batch_path,
                        ledger_path,
                        stage="publish",
                        run_at=f"2026-08-10T12:{attempt:02d}:00+08:00",
                        workflow_version="legacy-workflow",
                        error_code_override="publish_failed",
                        error_type_override="transient_failure",
                        retryable_override="true",
                        message_override="lark-cli docs 发布失败：发布恢复记录查询失败",
                    )

            before = ledger_path.read_bytes()
            ledger = json.loads(before)
            entries = list(ledger["entries"].values())
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(entry["status"] == "retry_exhausted" for entry in entries))
            fingerprint = entries[0]["error_fingerprint"]

            preview_stream = io.StringIO()
            with redirect_stdout(preview_stream):
                preview_result = MODULE.recover_stage_retries(
                    ledger_path,
                    stage="publish",
                    error_code="publish_failed",
                    from_workflow_version="legacy-workflow",
                    to_workflow_version="release-v2",
                    error_fingerprint=fingerprint,
                    expected_count=2,
                    run_at="2026-08-10T13:00:00+08:00",
                    apply=False,
                )
            self.assertEqual(preview_result, 0)
            preview = json.loads(preview_stream.getvalue())
            self.assertEqual(preview["matched_count"], 2)
            self.assertEqual(len(preview["target_entry_keys"]), 2)
            self.assertEqual(ledger_path.read_bytes(), before)

            fingerprint_mismatch_stream = io.StringIO()
            with redirect_stdout(fingerprint_mismatch_stream):
                fingerprint_mismatch_result = MODULE.recover_stage_retries(
                    ledger_path,
                    stage="publish",
                    error_code="publish_failed",
                    from_workflow_version="legacy-workflow",
                    to_workflow_version="release-v2",
                    error_fingerprint="0" * 64,
                    expected_count=2,
                    run_at="2026-08-10T13:00:00+08:00",
                    apply=True,
                )
            self.assertEqual(fingerprint_mismatch_result, 2)
            self.assertEqual(
                json.loads(fingerprint_mismatch_stream.getvalue())["error"],
                "expected_count_mismatch",
            )
            self.assertEqual(ledger_path.read_bytes(), before)

            mismatch_stream = io.StringIO()
            with redirect_stdout(mismatch_stream):
                mismatch_result = MODULE.recover_stage_retries(
                    ledger_path,
                    stage="publish",
                    error_code="publish_failed",
                    from_workflow_version="legacy-workflow",
                    to_workflow_version="release-v2",
                    error_fingerprint=fingerprint,
                    expected_count=3,
                    run_at="2026-08-10T13:00:00+08:00",
                    apply=True,
                )
            self.assertEqual(mismatch_result, 2)
            self.assertEqual(json.loads(mismatch_stream.getvalue())["error"], "expected_count_mismatch")
            self.assertEqual(ledger_path.read_bytes(), before)

            apply_stream = io.StringIO()
            with redirect_stdout(apply_stream):
                apply_result = MODULE.recover_stage_retries(
                    ledger_path,
                    stage="publish",
                    error_code="publish_failed",
                    from_workflow_version="legacy-workflow",
                    to_workflow_version="release-v2",
                    error_fingerprint=fingerprint,
                    expected_count=2,
                    run_at="2026-08-10T13:00:00+08:00",
                    apply=True,
                )
            self.assertEqual(apply_result, 0)
            self.assertTrue(json.loads(apply_stream.getvalue())["ledger_written"])
            after_apply = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(after_apply["recovery_audit"]), 1)
            original_entries = [
                entry
                for entry in after_apply["entries"].values()
                if entry["workflow_version"] == "legacy-workflow"
            ]
            released_entries = [
                entry
                for entry in after_apply["entries"].values()
                if entry["workflow_version"] == "release-v2"
            ]
            self.assertEqual(len(original_entries), 2)
            self.assertTrue(all(entry["status"] == "retry_exhausted" for entry in original_entries))
            self.assertEqual(len(released_entries), 2)
            self.assertTrue(all(entry["status"] == "recovery_released" for entry in released_entries))
            self.assertTrue(all(entry["failure_count"] == 0 for entry in released_entries))

            eligible_path = base / "eligible-release-v2.json"
            with redirect_stdout(io.StringIO()):
                MODULE.filter_stage_retries(
                    batch_path,
                    ledger_path,
                    eligible_path,
                    stage="publish",
                    run_at="2026-08-10T13:00:00+08:00",
                    workflow_version="release-v2",
                )
            eligible = json.loads(eligible_path.read_text(encoding="utf-8"))
            self.assertEqual([item["filename"] for item in eligible["files"]], ["legacy-a.pdf", "legacy-b.pdf"])

            second_stream = io.StringIO()
            with redirect_stdout(second_stream):
                second_result = MODULE.recover_stage_retries(
                    ledger_path,
                    stage="publish",
                    error_code="publish_failed",
                    from_workflow_version="legacy-workflow",
                    to_workflow_version="release-v2",
                    error_fingerprint=fingerprint,
                    expected_count=2,
                    run_at="2026-08-10T13:05:00+08:00",
                    apply=True,
                )
            self.assertEqual(second_result, 0)
            self.assertTrue(json.loads(second_stream.getvalue())["already_applied"])
            after_second_apply = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(after_second_apply["recovery_audit"]), 1)
            self.assertEqual(
                len(
                    [
                        entry
                        for entry in after_second_apply["entries"].values()
                        if entry["workflow_version"] == "release-v2"
                    ]
                ),
                2,
            )

    def test_stage_retry_ledger_uses_fixed_5_10_20_minute_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            pdf = base / "retry.pdf"
            pdf.write_bytes(b"retry")
            batch_path = base / "batch.json"
            ledger_path = base / "ledger.json"
            batch_path.write_text(
                json.dumps({"files": [{"path": str(pdf), "filename": pdf.name}]}),
                encoding="utf-8",
            )
            expected = ["08:05:00", "08:20:00", "08:50:00"]
            run_times = ["2026-07-14T08:00:00+08:00", "2026-07-14T08:10:00+08:00", "2026-07-14T08:30:00+08:00"]
            for run_at, expected_time in zip(run_times, expected, strict=True):
                with redirect_stdout(io.StringIO()):
                    MODULE.record_stage_retry(
                        batch_path,
                        ledger_path,
                        stage="summary",
                        run_at=run_at,
                        workflow_version="workflow-v2",
                        error_code_override="summary_timeout",
                        error_type_override="transient_failure",
                        retryable_override="true",
                        message_override="timeout",
                    )
                entry = next(iter(json.loads(ledger_path.read_text())["entries"].values()))
                self.assertIn(expected_time, entry["next_retry_at"])

    def test_notification_outbox_dedupes_and_retries_5_10_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            outbox_path = base / "outbox.json"
            message_path = base / "message.txt"
            message_path.write_text("completed 3/4", encoding="utf-8")
            for _ in range(2):
                with redirect_stdout(io.StringIO()):
                    MODULE.notification_outbox_enqueue(
                        outbox_path,
                        idempotency_key="same-transition",
                        supersede_scope="batch-a",
                        event="partial-success",
                        message_format="text",
                        message_file=message_path,
                        run_id="run-a",
                        run_at="2026-07-14T08:00:00+08:00",
                    )
            self.assertEqual(len(json.loads(outbox_path.read_text())["items"]), 1)

            run_times = ["2026-07-14T08:00:00+08:00", "2026-07-14T08:05:00+08:00", "2026-07-14T08:15:00+08:00"]
            expected = ["08:05:00", "08:15:00", "08:35:00"]
            for run_at, expected_time in zip(run_times, expected, strict=True):
                with redirect_stdout(io.StringIO()):
                    MODULE.notification_outbox_record(
                        outbox_path,
                        idempotency_key="same-transition",
                        run_at=run_at,
                        status="failed",
                        message_id="",
                        error="network",
                    )
                item = json.loads(outbox_path.read_text())["items"]["same-transition"]
                self.assertIn(expected_time, item["next_attempt_at"])

            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_record(
                    outbox_path,
                    idempotency_key="same-transition",
                    run_at="2026-07-14T08:35:00+08:00",
                    status="success",
                    message_id="om_test",
                    error="",
                )
            item = json.loads(outbox_path.read_text())["items"]["same-transition"]
            self.assertEqual(item["status"], "sent")
            self.assertEqual(item["message_id"], "om_test")

    def test_notification_outbox_keeps_document_before_terminal_for_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            outbox_path = base / "outbox.json"
            message_path = base / "message.txt"

            message_path.write_text("document ready", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_enqueue(
                    outbox_path,
                    idempotency_key="document-ready",
                    supersede_scope="document-a",
                    event="doc-completed",
                    message_format="markdown",
                    message_file=message_path,
                    run_id="run-a",
                    run_at="2026-07-14T08:00:00+08:00",
                )
                MODULE.notification_outbox_record(
                    outbox_path,
                    idempotency_key="document-ready",
                    run_at="2026-07-14T08:00:00+08:00",
                    status="failed",
                    message_id="",
                    error="network",
                )

            message_path.write_text("completed", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_enqueue(
                    outbox_path,
                    idempotency_key="completed",
                    supersede_scope="batch-a",
                    event="completed",
                    message_format="markdown",
                    message_file=message_path,
                    run_id="run-a",
                    run_at="2026-07-14T08:01:00+08:00",
                )

            items = json.loads(outbox_path.read_text(encoding="utf-8"))["items"]
            self.assertEqual(items["document-ready"]["status"], "pending")
            self.assertEqual(items["completed"]["status"], "pending")

            with redirect_stdout(io.StringIO()) as output:
                MODULE.notification_outbox_next_due(
                    outbox_path,
                    "2026-07-14T08:01:00+08:00",
                )
            self.assertFalse(json.loads(output.getvalue())["found"])

            with redirect_stdout(io.StringIO()) as output:
                MODULE.notification_outbox_next_due(
                    outbox_path,
                    "2026-07-14T08:05:00+08:00",
                )
            due = json.loads(output.getvalue())
            self.assertEqual(due["item"]["idempotency_key"], "document-ready")

            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_record(
                    outbox_path,
                    idempotency_key="document-ready",
                    run_at="2026-07-14T08:05:00+08:00",
                    status="success",
                    message_id="om_document",
                    error="",
                )
            with redirect_stdout(io.StringIO()) as output:
                MODULE.notification_outbox_next_due(
                    outbox_path,
                    "2026-07-14T08:05:00+08:00",
                )
            due = json.loads(output.getvalue())
            self.assertEqual(due["item"]["idempotency_key"], "completed")

    def test_notification_outbox_new_terminal_state_supersedes_pending_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            outbox_path = base / "outbox.json"
            message_path = base / "message.txt"
            message_path.write_text("failed", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_enqueue(
                    outbox_path,
                    idempotency_key="failed-root-a",
                    supersede_scope="batch-a",
                    event="failed",
                    message_format="markdown",
                    message_file=message_path,
                    run_id="run-a",
                    run_at="2026-07-14T08:00:00+08:00",
                )
                MODULE.notification_outbox_record(
                    outbox_path,
                    idempotency_key="failed-root-a",
                    run_at="2026-07-14T08:00:00+08:00",
                    status="failed",
                    message_id="",
                    error="network",
                )

            message_path.write_text("completed", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                MODULE.notification_outbox_enqueue(
                    outbox_path,
                    idempotency_key="completed",
                    supersede_scope="batch-a",
                    event="completed",
                    message_format="markdown",
                    message_file=message_path,
                    run_id="run-b",
                    run_at="2026-07-14T08:02:00+08:00",
                )

            payload = json.loads(outbox_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["items"]["failed-root-a"]["status"], "superseded")
            self.assertEqual(payload["items"]["failed-root-a"]["superseded_by"], "completed")
            self.assertEqual(payload["items"]["completed"]["status"], "pending")
            with redirect_stdout(io.StringIO()) as output:
                MODULE.notification_outbox_next_due(
                    outbox_path,
                    "2026-07-14T08:02:00+08:00",
                )
            due = json.loads(output.getvalue())
            self.assertEqual(due["item"]["idempotency_key"], "completed")


if __name__ == "__main__":
    unittest.main()

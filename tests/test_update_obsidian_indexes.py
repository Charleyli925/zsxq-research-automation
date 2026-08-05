from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "update_obsidian_indexes.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("update_obsidian_indexes", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class UpdateObsidianIndexesTests(unittest.TestCase):
    def test_incremental_only_aliases_select_same_mode(self) -> None:
        for option in ("--incremental-only", "--skip-full-rebuild"):
            with self.subTest(option=option), mock.patch.object(sys, "argv", ["update_obsidian_indexes.py", option]):
                args = MODULE.parse_args()
                self.assertTrue(args.incremental_only)
                self.assertFalse(args.rebuild_all)

    def test_incremental_only_main_skips_full_rebuild(self) -> None:
        args = argparse.Namespace(
            batch_file="",
            note=[],
            notes_file="",
            vault_root="/tmp/test-vault",
            result_file="",
            rebuild_all=False,
            incremental_only=True,
            recent_days=7,
            dry_run=False,
        )
        incremental_result = {"processed_note_count": 1}
        with (
            mock.patch.object(MODULE, "parse_args", return_value=args),
            mock.patch.object(MODULE, "collect_note_paths", return_value=[Path("/tmp/report.md")]),
            mock.patch.object(MODULE, "update_indexes", return_value=incremental_result) as update_indexes,
            mock.patch.object(MODULE, "rebuild_all_indexes") as rebuild_all_indexes,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(MODULE.main(), 0)

        update_indexes.assert_called_once()
        rebuild_all_indexes.assert_not_called()

    def test_default_incremental_command_keeps_legacy_full_rebuild(self) -> None:
        args = argparse.Namespace(
            batch_file="",
            note=[],
            notes_file="",
            vault_root="/tmp/test-vault",
            result_file="",
            rebuild_all=False,
            incremental_only=False,
            recent_days=7,
            dry_run=False,
        )
        with (
            mock.patch.object(MODULE, "parse_args", return_value=args),
            mock.patch.object(MODULE, "collect_note_paths", return_value=[]),
            mock.patch.object(MODULE, "update_indexes", return_value={"processed_note_count": 0}),
            mock.patch.object(MODULE, "rebuild_all_indexes", return_value={"report_note_count": 0}) as rebuild_all,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(MODULE.main(), 0)

        rebuild_all.assert_called_once_with(
            vault_root=Path("/tmp/test-vault").resolve(strict=False),
            dry_run=False,
            recent_days=7,
        )

    def test_ascii_alias_requires_token_boundary(self) -> None:
        self.assertFalse(MODULE.contains_keyword("LPDDR5X prices rose", "PDD"))
        self.assertFalse(MODULE.contains_keyword("AIDC demand rose", "AI"))
        self.assertTrue(MODULE.contains_keyword("拼多多（PDD-US）", "PDD"))
        self.assertTrue(MODULE.contains_keyword("AMD-US earnings", "AMD"))

    def test_rebuild_dedupes_and_removes_pdd_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "ResearchVault"
            reports = vault / "10_Reports" / "batch"
            companies = vault / "30_Companies"
            themes = vault / "20_Themes"
            reports.mkdir(parents=True)
            companies.mkdir(parents=True)
            themes.mkdir(parents=True)

            (themes / "存储周期.md").write_text(
                "---\nreport_count: 0\n---\n\n# 存储周期\n\n## 我的当前判断\n- \n\n## 自动收录报告（0）\n",
                encoding="utf-8",
            )
            (themes / "中国互联网平台.md").write_text(
                "---\nreport_count: 0\n---\n\n# 中国互联网平台\n\n## 我的当前判断\n- \n\n## 自动收录报告（0）\n",
                encoding="utf-8",
            )
            (themes / "主题索引.md").write_text("# 主题索引\n", encoding="utf-8")
            (companies / "拼多多.md").write_text(
                "---\nreport_count: 0\n---\n\n# 拼多多\n\n## 我的当前判断\n- \n\n## 关键跟踪点\n- \n\n## 别名\n- 拼多多、PDD\n\n## 相关报告（0）\n",
                encoding="utf-8",
            )
            (companies / "公司索引.md").write_text(
                "# 公司索引\n\n- [[30_Companies/拼多多|拼多多]]（0）\n",
                encoding="utf-8",
            )
            (vault / "00_首页.md").write_text("# ResearchVault 首页\n", encoding="utf-8")

            (reports / "memory.md").write_text(
                "---\n"
                "type: report\n"
                "report_id: \"r_memory\"\n"
                "title: \"韩国科技内存价格追踪\"\n"
                "downloaded_at: \"2026-06-01T10:00:00+08:00\"\n"
                "pdf_path: \"file:///tmp/memory.pdf\"\n"
                "---\n\n"
                "# 韩国科技内存价格追踪\n\n"
                "LPDDR5X 价格上涨，LPDDR5X 供给紧张。\n",
                encoding="utf-8",
            )
            (reports / "pdd-old.md").write_text(
                "---\n"
                "type: report\n"
                "report_id: \"r_pdd\"\n"
                "title: \"拼多多（PDD-US）：进入投资周期\"\n"
                "downloaded_at: \"2026-05-30T10:00:00+08:00\"\n"
                "pdf_path: \"file:///tmp/pdd.pdf\"\n"
                "---\n\n"
                "# 拼多多（PDD-US）：进入投资周期\n\n"
                "拼多多 PDD Temu 电商。\n",
                encoding="utf-8",
            )
            (reports / "pdd-new.md").write_text(
                "---\n"
                "type: report\n"
                "report_id: \"r_pdd\"\n"
                "title: \"拼多多（PDD-US）：进入新的投资周期\"\n"
                "downloaded_at: \"2026-06-01T11:00:00+08:00\"\n"
                "pdf_path: \"file:///tmp/pdd.pdf\"\n"
                "---\n\n"
                "# 拼多多（PDD-US）：进入新的投资周期\n\n"
                "拼多多 PDD Temu 电商。\n",
                encoding="utf-8",
            )

            result = MODULE.rebuild_all_indexes(vault, dry_run=False, recent_days=7)

            pdd_text = (companies / "拼多多.md").read_text(encoding="utf-8")
            self.assertIn("report_count: 1", pdd_text)
            self.assertIn("pdd-new", pdd_text)
            self.assertNotIn("memory", pdd_text)
            self.assertEqual(result["duplicate_group_count"], 1)
            self.assertTrue((vault / "99_维护" / "数据质量清单.md").exists())


if __name__ == "__main__":
    unittest.main()

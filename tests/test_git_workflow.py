from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_repository_hygiene import iter_repository_files
from scripts.git_workflow import WorkflowError, select_test_targets, validate_exact_scope


ROOT = Path(__file__).resolve().parents[1]


class GitWorkflowTests(unittest.TestCase):
    def init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
        (path / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)

    def test_exact_scope_accepts_all_and_rejects_an_omitted_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            self.init_repo(repo)
            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            (repo / "new.txt").write_text("new\n", encoding="utf-8")

            self.assertEqual(validate_exact_scope(repo, ["tracked.txt", "new.txt"]), ["new.txt", "tracked.txt"])
            with self.assertRaisesRegex(WorkflowError, "new.txt"):
                validate_exact_scope(repo, ["tracked.txt"])

    def test_hygiene_uses_git_visible_files_and_skips_known_local_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            self.init_repo(repo)
            (repo / "draft.md").write_text("visible\n", encoding="utf-8")
            local_only = repo / "local-only"
            local_only.mkdir()
            (local_only / "config.env").write_text("private\n", encoding="utf-8")
            with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
                handle.write("local-only/\n")

            visible = {path.relative_to(repo).as_posix() for path in iter_repository_files(repo)}

            self.assertIn("tracked.txt", visible)
            self.assertIn("draft.md", visible)
            self.assertNotIn("local-only/config.env", visible)

    def test_workflow_selects_a_focused_test_for_its_own_changes(self) -> None:
        self.assertEqual(select_test_targets(ROOT, ["scripts/git_workflow.py"]), ["tests/test_git_workflow.py"])

    def test_workflow_maps_direct_download_and_config_changes_to_their_contract_tests(self) -> None:
        self.assertEqual(
            select_test_targets(ROOT, ["src/zsxq_pipeline/download.py"]),
            [
                "tests/test_download_zsxq_plan_file.py",
                "tests/test_finalize_download_batch.py",
                "tests/test_pipeline_browser.py",
                "tests/test_pipeline_download.py",
                "tests/test_pipeline_download_result.py",
                "tests/test_scan_zsxq_download_candidates.py",
                "tests/test_zsxq_preflight.py",
            ],
        )
        self.assertEqual(select_test_targets(ROOT, ["src/zsxq_pipeline/config.py"]), ["tests/test_pipeline_cli.py"])


if __name__ == "__main__":
    unittest.main()

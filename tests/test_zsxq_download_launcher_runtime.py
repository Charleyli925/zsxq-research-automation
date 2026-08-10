from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "openclaw_tasks" / "zsxq_download" / "run.sh"


class ZsxqDownloadLauncherRuntimeTests(unittest.TestCase):
    def _task(self, base: Path, runner_script: str) -> Path:
        task = base / "task"
        task.mkdir()
        fake_runner = base / "runner.py"
        fake_runner.write_text(runner_script, encoding="utf-8")
        legacy_state = base / "legacy.json"
        legacy_state.write_text('{"last_successful_check_at":"2026-08-10T08:00:00+00:00"}\n', encoding="utf-8")
        (task / "config.env").write_text(
            "\n".join(
                [
                    f"AUTOMATION_ROOT={ROOT}",
                    f"DOWNLOAD_RUNNER_PATH={fake_runner}",
                    "ZSXQ_SOURCE_NAME=foreign_reports",
                    f"ZSXQ_JOB_CONFIG_FILE={base / 'job.json'}",
                    f"ZSXQ_KEYWORDS_FILE={base / 'keywords.json'}",
                    f"ZSXQ_LEGACY_STATE_FILE={legacy_state}",
                    f"INVESTMENT_REPORTS_RUNTIME_DIR={base / 'runtime'}",
                    "CFT_EXECUTABLE_PATH=/bin/true",
                    f"CFT_USER_DATA_DIR={base / 'cft-profile'}",
                    "NOTIFICATION_POLICY_PATH=/missing/notification.py",
                    "NOTIFICATION_PIPELINE=foreign_download",
                    "PYTHON_BIN=" + sys.executable,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return task

    def _run(self, task: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["ZSXQ_RUNTIME_TASK_DIR"] = str(task)
        return subprocess.run(["bash", str(RUNNER)], cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    def test_compatibility_runner_invokes_deterministic_python_entrypoint_and_writes_current_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            base = Path(raw_tmp)
            task = self._task(
                base,
                """import json, sys
from pathlib import Path
args = sys.argv[1:]
result_path = Path(args[args.index('--result-path') + 1])
result_path.parent.mkdir(parents=True, exist_ok=True)
result_path.write_text(json.dumps({'status':'success','reason_code':'download_completed','downloaded_count':0,'downloaded_files':[],'scan_plan_path':'/fixture/plan.json','scan_plan_hash':'a'*64}) + '\\n', encoding='utf-8')
print(json.dumps({'argv': args}))
""",
            )
            completed = self._run(task)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((task / "last_result.json").read_text(encoding="utf-8"))
            status = json.loads((task / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "success")
            self.assertEqual(status["message"], "download_completed")
            self.assertEqual(status["scan_plan_hash"], "a" * 64)
            output = (task / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("--source", output)
            self.assertIn("foreign_reports", output)
            self.assertIn("--cft-executable", output)
            self.assertIn("--cft-user-data-dir", output)

    def test_runner_creates_current_failure_result_when_deterministic_entrypoint_fails_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            task = self._task(Path(raw_tmp), "raise SystemExit(7)\n")
            completed = self._run(task)

            self.assertEqual(completed.returncode, 7, completed.stderr)
            result = json.loads((task / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["process_exit_code"], 7)
            self.assertEqual(result["reason_code"], "pipeline_runner_failed")


if __name__ == "__main__":
    unittest.main()

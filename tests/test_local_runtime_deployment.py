from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install_local_runtime.sh"
FOREIGN_TEMPLATE = ROOT / "deploy" / "launchd" / "zsxq-autodownload.plist.template"
DOMESTIC_TEMPLATE = ROOT / "deploy" / "launchd" / "zsxq-domestic-cicc.plist.template"
RUNNER = ROOT / "openclaw_tasks" / "zsxq_download" / "run.sh"
CRON_WRAPPER = ROOT / "openclaw_tasks" / "zsxq_download" / "run.cron-safe.sh"
FOREIGN_LAUNCHER = ROOT / "scripts" / "run_zsxq_task_via_codex.sh"
DOMESTIC_LAUNCHER = ROOT / "scripts" / "run_zsxq_domestic_cicc_task_via_codex.sh"
DIGEST_RUNNER = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.sh"
DIGEST_CRON_WRAPPER = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.cron-safe.sh"


class LocalRuntimeDeploymentTests(unittest.TestCase):
    def make_task(self, tasks_root: Path, name: str) -> Path:
        task_dir = tasks_root / name
        task_dir.mkdir(parents=True)
        (task_dir / "config.env").write_text('TARGET_CHAT_ID="private-chat-id"\n', encoding="utf-8")
        (task_dir / "run.sh").write_text("old runner\n", encoding="utf-8")
        (task_dir / "run.cron-safe.sh").write_text("old cron wrapper\n", encoding="utf-8")
        return task_dir

    def test_launchagent_templates_have_recovery_and_no_keepalive_loop(self) -> None:
        expected_minutes = {FOREIGN_TEMPLATE: [0, 0, 0, 30], DOMESTIC_TEMPLATE: [20, 20, 20, 50]}
        for template, minutes in expected_minutes.items():
            payload = plistlib.loads(template.read_bytes())
            self.assertEqual(payload["Label"], "__LABEL__")
            self.assertTrue(payload["RunAtLoad"])
            self.assertEqual(payload["ThrottleInterval"], 60)
            self.assertNotIn("KeepAlive", payload)
            self.assertEqual([entry["Minute"] for entry in payload["StartCalendarInterval"]], minutes)
            self.assertEqual(payload["ProgramArguments"][-1], "__TASK_DIR__/run.cron-safe.sh")

    def test_dry_run_validates_without_changing_task_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            tasks_root = tmp / "tasks"
            foreign = self.make_task(tasks_root, "ZSXQ_autodownload")
            domestic = self.make_task(tasks_root, "ZSXQ_国内研报_中金公司")
            digest = self.make_task(tasks_root, "ZSXQ_pdf_digest")
            completed = subprocess.run(
                [
                    "bash",
                    str(INSTALLER),
                    "--dry-run",
                    "--tasks-root",
                    str(tasks_root),
                    "--foreign-label",
                    "com.test.foreign",
                    "--domestic-label",
                    "com.test.domestic",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("dry run complete", completed.stdout)
            self.assertEqual((foreign / "run.sh").read_text(encoding="utf-8"), "old runner\n")
            self.assertEqual((domestic / "run.cron-safe.sh").read_text(encoding="utf-8"), "old cron wrapper\n")
            self.assertEqual((digest / "run.sh").read_text(encoding="utf-8"), "old runner\n")

    def test_apply_links_release_wrappers_and_keeps_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            tasks_root = tmp / "tasks"
            foreign = self.make_task(tasks_root, "ZSXQ_autodownload")
            domestic = self.make_task(tasks_root, "ZSXQ_国内研报_中金公司")
            digest = self.make_task(tasks_root, "ZSXQ_pdf_digest")
            completed = subprocess.run(
                [
                    "bash",
                    str(INSTALLER),
                    "--apply",
                    "--allow-dirty",
                    "--allow-branch",
                    "--skip-launchd",
                    "--tasks-root",
                    str(tasks_root),
                    "--foreign-label",
                    "com.test.foreign",
                    "--domestic-label",
                    "com.test.domestic",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            for task_dir in (foreign, domestic):
                self.assertTrue((task_dir / "run.sh").is_symlink())
                self.assertEqual((task_dir / "run.sh").resolve(), RUNNER.resolve())
                self.assertTrue((task_dir / "run.cron-safe.sh").is_symlink())
                self.assertEqual((task_dir / "run.cron-safe.sh").resolve(), CRON_WRAPPER.resolve())
                deployment_env = (task_dir / "deployment.env").read_text(encoding="utf-8")
                self.assertIn("AUTOMATION_ROOT=", deployment_env)
                self.assertNotIn("private-chat-id", deployment_env)
                self.assertIn("private-chat-id", (task_dir / "config.env").read_text(encoding="utf-8"))
                backup_root = task_dir / ".deployment-backups"
                self.assertEqual(len(list(backup_root.rglob("run.sh"))), 1)
                self.assertEqual(len(list(backup_root.rglob("run.cron-safe.sh"))), 1)

            self.assertTrue((digest / "run.sh").is_symlink())
            self.assertEqual((digest / "run.sh").resolve(), DIGEST_RUNNER.resolve())
            self.assertTrue((digest / "run.cron-safe.sh").is_symlink())
            self.assertEqual((digest / "run.cron-safe.sh").resolve(), DIGEST_CRON_WRAPPER.resolve())
            digest_deployment_env = (digest / "deployment.env").read_text(encoding="utf-8")
            self.assertNotIn("private-chat-id", digest_deployment_env)
            self.assertNotIn("TARGET_CHAT_ID", digest_deployment_env)
            self.assertNotIn("CODEX_SCRIPT_PATH", digest_deployment_env)
            expected_digest_keys = {
                "AUTOMATION_ROOT",
                "HELPER_SCRIPT_PATH",
                "SCANNER_SCRIPT_PATH",
                "RESEARCH_LIBRARY_INDEX_SCRIPT_PATH",
                "MARKITDOWN_SCRIPT_PATH",
                "CLEAN_MARKDOWN_SCRIPT_PATH",
                "OBSIDIAN_ARCHIVE_SCRIPT_PATH",
                "OBSIDIAN_INDEX_SCRIPT_PATH",
                "RUNTIME_GUARD_SCRIPT_PATH",
            }
            actual_digest_keys = {
                line.split("=", 1)[0]
                for line in digest_deployment_env.splitlines()
                if line and not line.startswith("#")
            }
            self.assertEqual(actual_digest_keys, expected_digest_keys)
            self.assertTrue(all(str(ROOT) in line for line in digest_deployment_env.splitlines() if "=" in line))
            digest_backup_root = digest / ".deployment-backups"
            self.assertEqual(len(list(digest_backup_root.rglob("run.sh"))), 1)
            self.assertEqual(len(list(digest_backup_root.rglob("run.cron-safe.sh"))), 1)

            foreign_deployment_env = (foreign / "deployment.env").read_text(encoding="utf-8")
            domestic_deployment_env = (domestic / "deployment.env").read_text(encoding="utf-8")
            self.assertIn(f"CODEX_SCRIPT_PATH={shlex.quote(str(FOREIGN_LAUNCHER))}", foreign_deployment_env)
            self.assertIn(f"CODEX_SCRIPT_PATH={shlex.quote(str(DOMESTIC_LAUNCHER))}", domestic_deployment_env)
            self.assertNotIn(f"CODEX_SCRIPT_PATH={shlex.quote(str(RUNNER))}", foreign_deployment_env)
            self.assertNotIn(f"CODEX_SCRIPT_PATH={shlex.quote(str(RUNNER))}", domestic_deployment_env)

            record = json.loads(
                (tasks_root / ".deployment" / "investment-reports-automation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["tasks"]["foreign_download"]["label"], "com.test.foreign")
            self.assertEqual(record["tasks"]["domestic_cicc"]["label"], "com.test.domestic")
            self.assertEqual(record["tasks"]["pdf_digest"]["task_dir"], str(digest.resolve()))
            self.assertEqual(record["tasks"]["pdf_digest"]["scheduler"], "cron")

    def test_apply_refuses_when_any_runtime_task_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            tasks_root = tmp / "tasks"
            task_dirs = {
                "foreign": self.make_task(tasks_root, "ZSXQ_autodownload"),
                "domestic": self.make_task(tasks_root, "ZSXQ_国内研报_中金公司"),
                "digest": self.make_task(tasks_root, "ZSXQ_pdf_digest"),
            }
            for name, active_task in task_dirs.items():
                with self.subTest(task=name):
                    canonical_task = active_task.resolve()
                    process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)", str(canonical_task / "run.sh")]
                    )
                    try:
                        (active_task / ".run.pid").write_text(str(process.pid) + "\n", encoding="utf-8")
                        completed = subprocess.run(
                            [
                                "bash",
                                str(INSTALLER),
                                "--apply",
                                "--allow-dirty",
                                "--allow-branch",
                                "--skip-launchd",
                                "--tasks-root",
                                str(tasks_root),
                                "--foreign-label",
                                "com.test.foreign",
                                "--domestic-label",
                                "com.test.domestic",
                            ],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                    finally:
                        process.terminate()
                        process.wait(timeout=10)
                        (active_task / ".run.pid").unlink(missing_ok=True)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(str(canonical_task), completed.stderr)
                    self.assertFalse((active_task / "deployment.env").exists())

    def test_runner_creates_current_failure_result_when_launcher_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            task_dir = tmp / "task"
            task_dir.mkdir()
            (task_dir / "run.sh").symlink_to(RUNNER)
            canonical = tmp / "canonical.json"
            config = "\n".join(
                [
                    f"AUTOMATION_ROOT={shlex.quote(str(ROOT))}",
                    f"CODEX_SCRIPT_PATH={shlex.quote(str(tmp / 'missing-launcher.sh'))}",
                    f"CODEX_STRUCTURED_REPORT_PATH={shlex.quote(str(canonical))}",
                    'NOTIFICATION_PIPELINE="foreign_download"',
                    f"NOTIFICATION_POLICY_PATH={shlex.quote(str(tmp / 'missing-policy.py'))}",
                    'LOG_FILE="cron.log"',
                    'RESULT_JSON="last_result.json"',
                    'RESULT_MD="last_result.md"',
                    "",
                ]
            )
            (task_dir / "config.env").write_text(config, encoding="utf-8")
            env = os.environ.copy()
            completed = subprocess.run(
                ["bash", str(task_dir / "run.sh")],
                cwd=task_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            result = json.loads((task_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["run_id"])
            canonical_result = json.loads(canonical.read_text(encoding="utf-8"))
            self.assertEqual(canonical_result["run_id"], result["run_id"])
            self.assertEqual(canonical_result["status"], "failed")
            self.assertTrue(canonical_result["recovered_stale_result"])

    def test_runner_refuses_nested_invocation_without_touching_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            task_dir = tmp / "task"
            task_dir.mkdir()
            (task_dir / "run.sh").symlink_to(RUNNER)

            env = os.environ.copy()
            env["ZSXQ_OUTER_RUNNER_ACTIVE"] = "1"
            completed = subprocess.run(
                ["bash", str(task_dir / "run.sh")],
                cwd=task_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 75, completed.stderr)
            self.assertIn("refusing nested ZSXQ outer-runner invocation", completed.stderr)
            self.assertFalse((task_dir / "startup_debug.log").exists())
            self.assertFalse((task_dir / "last_result.json").exists())

    def test_cron_wrapper_passes_task_directory_to_an_overridden_runner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            source_dir = tmp / "release"
            task_dir = tmp / "task"
            source_dir.mkdir()
            task_dir.mkdir()
            source_runner = source_dir / "run.sh"
            source_cron = source_dir / "run.cron-safe.sh"
            source_runner.write_text(RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
            source_cron.write_text(CRON_WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
            source_runner.chmod(0o755)
            source_cron.chmod(0o755)
            (task_dir / "run.cron-safe.sh").symlink_to(source_cron)
            canonical = tmp / "canonical.json"
            config = "\n".join(
                [
                    f"AUTOMATION_ROOT={shlex.quote(str(ROOT))}",
                    f"CODEX_SCRIPT_PATH={shlex.quote(str(tmp / 'missing-launcher.sh'))}",
                    f"CODEX_STRUCTURED_REPORT_PATH={shlex.quote(str(canonical))}",
                    f"TASK_RUN_ENTRY_PATH={shlex.quote(str(source_runner))}",
                    f"PYTHON_BIN={shlex.quote(sys.executable)}",
                    'NOTIFICATION_PIPELINE="foreign_download"',
                    f"NOTIFICATION_POLICY_PATH={shlex.quote(str(tmp / 'missing-policy.py'))}",
                    'LOG_FILE="cron.log"',
                    'RESULT_JSON="last_result.json"',
                    'RESULT_MD="last_result.md"',
                    "",
                ]
            )
            (task_dir / "config.env").write_text(config, encoding="utf-8")

            completed = subprocess.run(
                ["bash", str(task_dir / "run.cron-safe.sh")],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            result = json.loads((task_dir / "last_result.json").read_text(encoding="utf-8"))
            canonical_result = json.loads(canonical.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(canonical_result["run_id"], result["run_id"])
            self.assertEqual(canonical_result["status"], "failed")
            self.assertFalse((source_dir / "startup_debug.log").exists())


if __name__ == "__main__":
    unittest.main()

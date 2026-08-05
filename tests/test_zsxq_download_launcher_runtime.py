from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FOREIGN_LAUNCHER = ROOT / "scripts" / "run_zsxq_task_via_codex.sh"
DOMESTIC_LAUNCHER = ROOT / "scripts" / "run_zsxq_domestic_cicc_task_via_codex.sh"


class ZsxqDownloadLauncherRuntimeTests(unittest.TestCase):
    def run_launcher(self, launcher: Path) -> tuple[subprocess.CompletedProcess[str], dict, dict]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            base = Path(raw_tmp)
            job_config = base / "job.json"
            keywords = base / "keywords.json"
            canonical = base / "canonical.json"
            status = base / "status.json"
            job_config.write_text(
                json.dumps(
                    {
                        "group_url": "https://wx.zsxq.com/group/test-group",
                        "tag_url": "https://wx.zsxq.com/tags/test-tag/test-id",
                    }
                ),
                encoding="utf-8",
            )
            keywords.write_text(json.dumps({"keywords": ["test"]}), encoding="utf-8")

            env = os.environ.copy()
            env.pop("CFT_START_URL", None)
            env.update(
                {
                    "CODEX_BIN": str(base / "missing-codex"),
                    "ZSXQ_JOB_CONFIG_FILE": str(job_config),
                    "ZSXQ_KEYWORDS_FILE": str(keywords),
                    "ZSXQ_STRUCTURED_RESULT_PATH": str(canonical),
                    "ZSXQ_STATUS_JSON_PATH": str(status),
                    "ZSXQ_LOG_DIR": str(base / "logs"),
                    "INVESTMENT_REPORTS_RUNTIME_DIR": str(base / "runtime"),
                }
            )
            completed = subprocess.run(
                ["bash", str(launcher)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            canonical_payload = json.loads(canonical.read_text(encoding="utf-8"))
            status_payload = json.loads(status.read_text(encoding="utf-8"))
            return completed, canonical_payload, status_payload

    def test_foreign_launcher_derives_start_url_and_writes_current_failure(self) -> None:
        completed, canonical, status = self.run_launcher(FOREIGN_LAUNCHER)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(status["message"], "codex binary missing or not executable")
        self.assertEqual(status["run_id"], canonical["run_id"])
        self.assertEqual(canonical["status"], "failed")
        self.assertEqual(canonical["codex_exit_code"], 2)

    def test_domestic_launcher_does_not_short_circuit_before_result_finalization(self) -> None:
        completed, canonical, status = self.run_launcher(DOMESTIC_LAUNCHER)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(status["message"], "codex binary missing or not executable")
        self.assertEqual(status["run_id"], canonical["run_id"])
        self.assertEqual(canonical["status"], "failed")


if __name__ == "__main__":
    unittest.main()

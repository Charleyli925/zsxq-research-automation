from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "zsxq_runtime_guard.py"


class ZsxqRuntimeGuardTests(unittest.TestCase):
    def run_guard(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def test_exec_timeout_streams_stdin_on_success(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            prompt = Path(raw_tmp) / "prompt.txt"
            prompt.write_text("hello\n", encoding="utf-8")
            completed = self.run_guard(
                "exec-timeout",
                "--timeout-seconds",
                "5",
                "--stdin-file",
                str(prompt),
                "--",
                "/bin/sh",
                "-c",
                'read value; printf "got:%s\\n" "$value"',
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "got:hello\n")

    def test_exec_timeout_terminates_process_group_and_returns_124(self) -> None:
        started_at = time.monotonic()
        completed = self.run_guard(
            "exec-timeout",
            "--timeout-seconds",
            "0.3",
            "--terminate-grace-seconds",
            "0.1",
            "--",
            "/bin/sh",
            "-c",
            'printf "started\\n"; sleep 5; printf "finished\\n"',
        )
        elapsed = time.monotonic() - started_at

        self.assertEqual(completed.returncode, 124, completed.stderr)
        self.assertLess(elapsed, 3)
        self.assertIn("started", completed.stdout)
        self.assertIn("ZSXQ_EXEC_TIMEOUT_JSON:", completed.stdout)
        self.assertNotIn("finished", completed.stdout)

    def test_lock_is_owned_rejects_competitor_and_requires_owner_token_to_release(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            lock_dir = Path(raw_tmp) / "task.lock"
            first = self.run_guard(
                "lock-acquire",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "owner-one",
                "--run-id",
                "run-one",
                "--owner-pid",
                str(os.getpid()),
            )
            second = self.run_guard(
                "lock-acquire",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "owner-two",
                "--run-id",
                "run-two",
                "--owner-pid",
                str(os.getpid()),
            )
            wrong_release = self.run_guard(
                "lock-release",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "owner-two",
            )
            release = self.run_guard(
                "lock-release",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "owner-one",
            )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 23, second.stderr)
        self.assertEqual(wrong_release.returncode, 3)
        self.assertEqual(release.returncode, 0, release.stderr)

    def test_dead_owner_lock_is_recovered_without_recursive_delete(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            lock_dir = Path(raw_tmp) / "task.lock"
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(
                json.dumps({"pid": 999999, "process_start": "dead", "token": "old"}) + "\n",
                encoding="utf-8",
            )

            acquired = self.run_guard(
                "lock-acquire",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "new-owner",
                "--run-id",
                "new-run",
                "--owner-pid",
                str(os.getpid()),
                "--stale-seconds",
                "300",
            )
            payload = json.loads(acquired.stdout)
            released = self.run_guard(
                "lock-release",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "new-owner",
            )

        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertTrue(payload["recovered_stale"])
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_lock_guard_refuses_symlink_without_touching_target(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            target = root / "target"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            lock_dir = root / "task.lock"
            lock_dir.symlink_to(target, target_is_directory=True)

            completed = self.run_guard(
                "lock-acquire",
                "--lock-dir",
                str(lock_dir),
                "--token",
                "owner",
                "--run-id",
                "run",
                "--owner-pid",
                str(os.getpid()),
                "--stale-seconds",
                "0",
            )

            self.assertEqual(completed.returncode, 2)
            self.assertTrue(lock_dir.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_prepare_model_cache_keeps_compatible_cache(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            codex = root / "codex"
            cache = root / "models_cache.json"
            codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.146.0\\n'\n", encoding="utf-8")
            codex.chmod(0o755)
            cache.write_text(json.dumps({"client_version": "0.146.0"}) + "\n", encoding="utf-8")

            completed = self.run_guard(
                "prepare-model-cache",
                "--codex-bin",
                str(codex),
                "--cache-file",
                str(cache),
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "compatible")

    def test_prepare_model_cache_quarantines_proven_version_mismatch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            codex = root / "codex"
            cache = root / "models_cache.json"
            codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.146.0\\n'\n", encoding="utf-8")
            codex.chmod(0o755)
            cache.write_text(json.dumps({"client_version": "0.144.1"}) + "\n", encoding="utf-8")

            completed = self.run_guard(
                "prepare-model-cache",
                "--codex-bin",
                str(codex),
                "--cache-file",
                str(cache),
            )
            payload = json.loads(completed.stdout)
            quarantine = Path(payload["quarantine"])

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["status"], "quarantined_incompatible")
            self.assertFalse(cache.exists())
            self.assertTrue(quarantine.exists())
            self.assertEqual(json.loads(quarantine.read_text(encoding="utf-8"))["client_version"], "0.144.1")


if __name__ == "__main__":
    unittest.main()

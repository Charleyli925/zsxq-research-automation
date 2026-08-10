"""Contract tests for the digest task's thin shell entry point.

The shell wrapper only resolves task-local configuration and delegates to the
Python pipeline.  A tiny executable captures its argv and environment, so
these tests never start the real pipeline or contact external services.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.sh"


class DigestRunnerContractTests(unittest.TestCase):
    maxDiff = None

    def write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def make_executable(self, path: Path) -> None:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_capture_interpreter(self, path: Path) -> None:
        self.write_file(
            path,
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys
                from pathlib import Path

                payload = {{
                    "argv": sys.argv[1:],
                    "automation_root": os.environ.get("AUTOMATION_ROOT"),
                    "marker": os.environ.get("PIPELINE_TEST_MARKER"),
                    "pythonpath": os.environ.get("PYTHONPATH"),
                }}
                Path(os.environ["CAPTURE_PATH"]).write_text(
                    json.dumps(payload, sort_keys=True), encoding="utf-8"
                )
                """
            ),
        )
        self.make_executable(path)

    def write_config(self, path: Path, *, interpreter: Path, marker: str) -> None:
        values = {
            "AUTOMATION_ROOT": str(ROOT),
            "PYTHON_BIN": str(interpreter),
            "PIPELINE_TEST_MARKER": marker,
        }
        self.write_file(
            path,
            "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n",
        )

    def make_source_and_runtime(self, base: Path) -> tuple[Path, Path]:
        release_root = base / "release"
        source_task = release_root / "openclaw_tasks" / "zsxq_pdf_digest"
        runtime_task = base / "runtime-task"
        source_task.mkdir(parents=True)
        (release_root / "src" / "zsxq_pipeline").mkdir(parents=True)
        runtime_task.mkdir()
        source_entry = source_task / "run.sh"
        shutil.copy2(RUN_SCRIPT, source_entry)
        self.make_executable(source_entry)
        (runtime_task / "run.sh").symlink_to(source_entry)
        return source_task, runtime_task

    def invoke(self, runtime_task: Path, capture_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "CAPTURE_PATH": str(capture_path),
                "PYTHONPATH": "/test/pythonpath",
            }
        )
        return subprocess.run(
            [str(runtime_task / "run.sh"), *args],
            cwd=str(cwd or runtime_task),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def read_capture(self, capture_path: Path) -> dict[str, object]:
        return json.loads(capture_path.read_text(encoding="utf-8"))

    def test_runtime_config_wins_and_forwards_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_task, runtime_task = self.make_source_and_runtime(base)
            source_interpreter = base / "source-python"
            runtime_interpreter = base / "runtime-python"
            self.write_capture_interpreter(source_interpreter)
            self.write_capture_interpreter(runtime_interpreter)
            self.write_config(source_task / "config.env", interpreter=source_interpreter, marker="source")
            self.write_config(runtime_task / "config.env", interpreter=runtime_interpreter, marker="runtime")
            capture_path = base / "capture.json"

            result = self.invoke(
                runtime_task,
                capture_path,
                "--preflight-only",
                "--dry-run",
                "--config",
                "/tmp/pipeline.toml",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(capture_path.is_file())
            self.assertEqual(
                self.read_capture(capture_path),
                {
                    "argv": [
                        "-m",
                        "zsxq_pipeline.cli",
                        "process",
                        "--runtime-root",
                        str(runtime_task.resolve()),
                        "--preflight-only",
                        "--dry-run",
                        "--config",
                        "/tmp/pipeline.toml",
                    ],
                    "automation_root": str(source_task.parents[1].resolve()),
                    "marker": "runtime",
                    "pythonpath": f"{source_task.parents[1].resolve() / 'src'}:/test/pythonpath",
                },
            )

    def test_source_config_is_the_only_fallback_when_runtime_config_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_task, runtime_task = self.make_source_and_runtime(base)
            source_interpreter = base / "source-python"
            cwd_interpreter = base / "cwd-python"
            self.write_capture_interpreter(source_interpreter)
            self.write_capture_interpreter(cwd_interpreter)
            self.write_config(source_task / "config.env", interpreter=source_interpreter, marker="source")

            unrelated_cwd = base / "unrelated-cwd"
            unrelated_cwd.mkdir()
            self.write_config(unrelated_cwd / "config.env", interpreter=cwd_interpreter, marker="cwd")
            capture_path = base / "capture.json"
            result = self.invoke(runtime_task, capture_path, "--preflight-only", cwd=unrelated_cwd)

            self.assertEqual(result.returncode, 0, result.stderr)
            captured = self.read_capture(capture_path)
            self.assertEqual(captured["marker"], "source")
            self.assertEqual(captured["argv"], [
                "-m",
                "zsxq_pipeline.cli",
                "process",
                "--runtime-root",
                str(runtime_task.resolve()),
                "--preflight-only",
            ])

    def test_missing_runtime_and_source_config_stops_before_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, runtime_task = self.make_source_and_runtime(base)
            capture_path = base / "capture.json"

            result = self.invoke(runtime_task, capture_path, "--preflight-only")

            self.assertEqual(result.returncode, 2)
            self.assertIn("缺少配置文件", result.stderr)
            self.assertFalse(capture_path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

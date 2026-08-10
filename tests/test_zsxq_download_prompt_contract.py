from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ZsxqDownloadRuntimeContractTests(unittest.TestCase):
    def test_download_runtime_is_plan_bound_and_does_not_depend_on_an_agent_or_dynamic_mcp(self) -> None:
        paths = [
            ROOT / "src" / "zsxq_pipeline" / "download.py",
            ROOT / "src" / "zsxq_pipeline" / "browser.py",
            ROOT / "scripts" / "scan_zsxq_download_candidates.py",
            ROOT / "scripts" / "download_zsxq_plan_file.py",
            ROOT / "openclaw_tasks" / "zsxq_download" / "run.sh",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()
        self.assertIn("plan_hash", text)
        self.assertIn("source_content_protected", text)
        self.assertIn("connect_over_cdp", text)
        self.assertNotIn("codex exec", text)
        self.assertNotIn("@playwright/mcp@latest", text)
        self.assertNotIn("run_zsxq_task_via_codex", text)

    def test_compatibility_wrapper_calls_the_release_owned_python_entrypoint(self) -> None:
        text = (ROOT / "openclaw_tasks" / "zsxq_download" / "run.sh").read_text(encoding="utf-8")
        self.assertIn("DOWNLOAD_RUNNER_PATH", text)
        self.assertIn("--result-path", text)
        self.assertIn("zsxq_pipeline", (ROOT / "scripts" / "run_zsxq_download_pipeline.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

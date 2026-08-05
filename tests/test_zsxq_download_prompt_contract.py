from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ZsxqDownloadPromptContractTests(unittest.TestCase):
    def test_task_templates_require_plan_bound_helper_and_manifest_proof(self) -> None:
        paths = [
            ROOT / "prompts" / "openclaw_task_template.md",
            ROOT / "prompts" / "openclaw_domestic_cicc_task_template.md",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("download_zsxq_plan_file.py", text)
                self.assertIn("source_content_protected", text)
                self.assertIn("do not replace the helper with MCP `browser_click`", text)
                self.assertIn("--wait-seconds 0", text)
                self.assertIn("Helper success is not archive proof", text)

    def test_scheduler_prompts_repeat_dynamic_page_guardrails(self) -> None:
        paths = [
            ROOT / "prompts" / "openclaw_scheduler_prompt.md",
            ROOT / "prompts" / "openclaw_domestic_cicc_scheduler_prompt.md",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("download_zsxq_plan_file.py", text)
                self.assertIn("source_content_protected", text)
                self.assertIn("--wait-seconds 0", text)

    def test_launcher_injects_same_contract_and_longer_action_timeout(self) -> None:
        text = (ROOT / "scripts" / "run_zsxq_task_via_codex.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'PLAYWRIGHT_ACTION_TIMEOUT_MS="${ZSXQ_PLAYWRIGHT_ACTION_TIMEOUT_MS:-20000}"',
            text,
        )
        self.assertIn(
            '\\"--timeout-action\\",\\"$PLAYWRIGHT_ACTION_TIMEOUT_MS\\"',
            text,
        )
        self.assertIn("download_zsxq_plan_file.py", text)
        self.assertIn("source_content_protected", text)
        self.assertIn("--wait-seconds 0", text)
        self.assertIn("zsxq_preflight.py", text)
        self.assertIn(
            'PREFLIGHT_NAVIGATION_ATTEMPTS="${ZSXQ_PREFLIGHT_NAVIGATION_ATTEMPTS:-3}"',
            text,
        )
        self.assertIn(
            'CFT_HEADLESS="${ZSXQ_CFT_HEADLESS:-true}"',
            text,
        )
        self.assertIn('"--headless=new"', text)
        self.assertIn('"--disable-extensions"', text)
        self.assertIn(
            '"--disable-component-extensions-with-background-pages"',
            text,
        )


if __name__ == "__main__":
    unittest.main()

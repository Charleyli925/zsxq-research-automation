from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "update_zsxq_focus.py"
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("update_zsxq_focus", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class UpdateZsxqFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.keywords_path = self.tmp_path / "interest_keywords.json"
        self.runtime_state_path = self.tmp_path / "runtime_state.json"
        self.runtime_prompt_path = self.tmp_path / "runtime_prompt.md"
        self.keywords_path.write_text(
            json.dumps(
                {
                    "interest_topics": ["AI"],
                    "match_keywords": ["AI"],
                    "exact_match_keywords": ["腾讯"],
                    "direct_topic_keywords": ["AI", "存储"],
                    "region_keywords": ["中国", "美国"],
                    "paired_topic_keywords": ["OTC"],
                    "exclude_keywords": [],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        self.runtime_state_path.write_text("{}\n", encoding="utf-8")
        self.runtime_prompt_path.write_text("# placeholder\n", encoding="utf-8")
        MODULE.KEYWORDS_PATH = self.keywords_path
        MODULE.RUNTIME_STATE_PATH = self.runtime_state_path
        MODULE.RUNTIME_PROMPT_PATH = self.runtime_prompt_path

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_persistent_add_writes_new_schema(self) -> None:
        with patch("sys.argv", [
            "update_zsxq_focus.py",
            "--scope", "persistent",
            "--action", "add",
            "--topic", "石油",
            "--keyword", "OpenAI",
        ]):
            MODULE.main()

        payload = json.loads(self.keywords_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn("石油", payload["interest_topics"])
        self.assertIn("石油", payload["standalone_keywords"])
        self.assertIn("OpenAI", payload["standalone_keywords"])
        self.assertNotIn("match_keywords", payload)

    def test_temporary_add_updates_runtime_state_and_prompt(self) -> None:
        with patch("sys.argv", [
            "update_zsxq_focus.py",
            "--scope", "temporary",
            "--action", "add",
            "--topic", "伊朗",
            "--note", "Keep an eye on energy reports.",
        ]):
            MODULE.main()

        runtime_state = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
        prompt_text = self.runtime_prompt_path.read_text(encoding="utf-8")
        self.assertTrue(runtime_state["temporary_focus_enabled"])
        self.assertIn("伊朗", runtime_state["temporary_focus"])
        self.assertIn("伊朗", prompt_text)
        self.assertIn("Keep an eye on energy reports.", prompt_text)

    def test_persistent_note_updates_prompt_snapshot(self) -> None:
        with patch("sys.argv", [
            "update_zsxq_focus.py",
            "--scope", "persistent",
            "--action", "add",
            "--note", "只看高优先级风险。",
        ]):
            MODULE.main()

        payload = json.loads(self.keywords_path.read_text(encoding="utf-8"))
        prompt_text = self.runtime_prompt_path.read_text(encoding="utf-8")
        self.assertIn("只看高优先级风险。", payload["notes"])
        self.assertIn("只看高优先级风险。", prompt_text)


if __name__ == "__main__":
    unittest.main()

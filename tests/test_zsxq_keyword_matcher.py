from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.zsxq_keyword_matcher import match_title


class ZsxqKeywordMatcherTests(unittest.TestCase):
    def test_example_exclusion_takes_priority_over_positive_keyword(self) -> None:
        payload_path = ROOT / "config" / "examples" / "domestic-keywords.example.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        excluded = match_title(
            "Example Broker generic weekly report.pdf",
            payload,
        )
        kept = match_title("Example Company deep dive.pdf", payload)

        self.assertIsNone(excluded.match_rule)
        self.assertEqual(excluded.matched_keywords, [])
        self.assertEqual(kept.match_rule, "standalone")
        self.assertEqual(kept.matched_keywords, ["Example Company"])

    def test_matches_new_standalone_keywords(self) -> None:
        payload = {
            "schema_version": 2,
            "standalone_keywords": ["腾讯", "OpenAI"],
            "region_keywords": ["中国", "美国"],
            "region_required_keywords": ["OTC"],
            "exclude_keywords": [],
        }

        result = match_title("腾讯广告观察", payload)

        self.assertEqual(result.match_rule, "standalone")
        self.assertEqual(result.matched_keywords, ["腾讯"])

    def test_matches_region_required_rule(self) -> None:
        payload = {
            "schema_version": 2,
            "standalone_keywords": ["腾讯"],
            "region_keywords": ["中国", "美国"],
            "region_required_keywords": ["OTC"],
            "exclude_keywords": [],
        }

        result = match_title("中国OTC市场周报", payload)

        self.assertEqual(result.match_rule, "region_plus_topic")
        self.assertEqual(result.matched_keywords, ["OTC", "中国"])

    def test_old_config_is_still_readable(self) -> None:
        payload = {
            "match_keywords": ["OpenAI", "中国"],
            "exact_match_keywords": ["腾讯"],
            "direct_topic_keywords": ["存储", "石油"],
            "region_keywords": ["中国", "美国"],
            "paired_topic_keywords": ["存储", "OTC"],
            "exclude_keywords": [],
        }

        result = match_title("美国石油与存储行业", payload)

        self.assertEqual(result.match_rule, "standalone")
        self.assertEqual(result.matched_keywords, ["存储", "石油"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "archive_to_obsidian.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("archive_to_obsidian", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ArchiveToObsidianTests(unittest.TestCase):
    def test_archive_batch_writes_note_and_updates_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            library_root = base / "ResearchLibrary"
            vault_root = base / "ResearchVault"
            pdf_path = base / "alpha.pdf"
            batch_id = "2026-05-07_10-00-00__to__2026-05-07_10-10-00"
            summary_path = library_root / "summaries" / batch_id / "alpha.summary.md"
            library_database = base / "runtime" / "research_library.sqlite"
            batch_path = base / "batch.json"
            pdf_path.write_bytes(b"%PDF-test")
            summary_path.parent.mkdir(parents=True)
            summary_path.write_text("# Alpha\n\n## Key points\n- One", encoding="utf-8")
            batch_path.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "report_id": "zsxq_aaaaaaaaaaaaaaaa",
                                "pdf_sha256": "a" * 64,
                                "path": str(pdf_path),
                                "filename": "alpha.pdf",
                                "batch_id": batch_id,
                                "modified_at": "2026-05-07T10:00:00+08:00",
                                "summary_md_path": str(summary_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = MODULE.archive_batch(
                batch_file=batch_path,
                library_root=library_root,
                vault_root=vault_root,
                feishu_doc_url="https://example.com/doc",
                library_database=library_database,
            )
            updated = json.loads(batch_path.read_text(encoding="utf-8"))["files"][0]
            note_path = Path(updated["obsidian_note_path"])

            self.assertEqual(result["archived_count"], 1)
            self.assertTrue(note_path.exists())
            self.assertEqual(note_path.parent.name, batch_id)
            self.assertEqual(note_path.name, "alpha.md")
            note_text = note_path.read_text(encoding="utf-8")
            self.assertIn("feishu_doc_url", note_text)
            self.assertIn("https://example.com/doc", note_text)
            self.assertIn("# Alpha", note_text)
            self.assertTrue(library_database.is_file())
            self.assertFalse((library_root / "state" / "processed_files.sqlite").exists())


if __name__ == "__main__":
    unittest.main()

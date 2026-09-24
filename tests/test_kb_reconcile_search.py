from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from kb_reconcile_search import reconcile  # noqa: E402


def test_reconcile_repairs_new_changed_and_removed_notes_through_symlink(tmp_path):
    vault = tmp_path / "vault"
    actual = tmp_path / "actual-reports"
    actual.mkdir()
    vault.mkdir()
    (vault / "10_Reports").symlink_to(actual, target_is_directory=True)
    config = tmp_path / "config"
    config.mkdir()
    db = tmp_path / "index.sqlite"
    note = actual / "September.md"
    note.write_text(
        "---\nreport_id: september_1\ntitle: 九月测试研报-260915\n---\n\n## 摘要\n首次结论\n",
        encoding="utf-8",
    )

    first = reconcile(db, vault, config)
    assert {key: first[key] for key in ("seen", "updated", "pending", "deleted", "errors", "complete")} == {
        "seen": 1, "updated": 1, "pending": 0, "deleted": 0, "errors": 0, "complete": True,
    }
    assert first["indexed"] == 1
    assert reconcile(db, vault, config)["updated"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM reports WHERE report_id='september_1'").fetchone()[0] == 1
    note.write_text(
        "---\nreport_id: september_1\ntitle: 九月测试研报-260915\n---\n\n## 摘要\n修订后的核心结论\n",
        encoding="utf-8",
    )
    assert reconcile(db, vault, config)["updated"] == 1
    with sqlite3.connect(db) as conn:
        text = conn.execute("SELECT summary_text FROM report_search WHERE report_id='september_1'").fetchone()[0]
    assert "修订后的核心结论" in text
    note.unlink()
    assert reconcile(db, vault, config)["deleted"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search").fetchone()[0] == 0


def test_reconcile_bounds_updates_and_resumes(tmp_path):
    vault = tmp_path / "vault"
    reports = vault / "10_Reports"
    reports.mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    for index in range(2):
        (reports / f"{index}.md").write_text(
            f"---\nreport_id: report_{index}\ntitle: 测试报告{index}-260915\n---\n\n## 摘要\n内容{index}\n",
            encoding="utf-8",
        )
    db = tmp_path / "index.sqlite"
    first = reconcile(db, vault, config, max_updates=1)
    assert first["updated"] == 1
    assert first["pending"] == 1
    assert first["complete"] is False
    second = reconcile(db, vault, config, max_updates=1)
    assert second["updated"] == 1
    assert second["complete"] is True


def test_reconcile_preserves_distinct_notes_with_duplicate_report_ids(tmp_path):
    vault = tmp_path / "vault"
    reports = vault / "10_Reports"
    reports.mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    for name in ("one", "two"):
        (reports / f"{name}.md").write_text(
            f"---\nreport_id: shared\ntitle: {name}-260915\n---\n\n## 摘要\n{name} conclusion\n",
            encoding="utf-8",
        )
    db = tmp_path / "index.sqlite"
    assert reconcile(db, vault, config)["updated"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search WHERE report_id='shared'").fetchone()[0] == 2
    (reports / "one.md").write_text(
        "---\nreport_id: shared\ntitle: one-260915\n---\n\n## 摘要\nchanged conclusion\n",
        encoding="utf-8",
    )
    assert reconcile(db, vault, config)["updated"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search WHERE report_id='shared'").fetchone()[0] == 2

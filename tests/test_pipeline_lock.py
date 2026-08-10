from __future__ import annotations

from zsxq_pipeline.lock import runtime_lock


def test_runtime_lock_is_nonblocking_and_released_on_context_exit(tmp_path):
    root = tmp_path / "runtime"
    with runtime_lock(root) as first:
        assert first is True
        with runtime_lock(root) as second:
            assert second is False
    with runtime_lock(root) as recovered:
        assert recovered is True
    assert (root / ".pipeline.lock").is_file()

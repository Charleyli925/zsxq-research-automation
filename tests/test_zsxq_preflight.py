from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from zsxq_pipeline.browser import BrowserDoctorResult, BrowserSessionError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "zsxq_preflight.py"


def _module():
    spec = importlib.util.spec_from_file_location("testable_zsxq_preflight", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv(config: Path) -> list[str]:
    return [
        str(SCRIPT_PATH),
        "--cdp-endpoint",
        "http://127.0.0.1:9223",
        "--start-url",
        "https://wx.zsxq.com/group/fixture",
        "--job-config",
        str(config),
        "--navigation-attempts",
        "1",
    ]


def test_preflight_uses_browser_session_doctor_for_a_ready_page(tmp_path, monkeypatch, capsys):
    config = tmp_path / "job.json"
    config.write_text(json.dumps({"group_name": "fixture"}), encoding="utf-8")
    module = _module()

    class FakeSession:
        def __init__(self, endpoint: str, **kwargs) -> None:
            assert endpoint == "http://127.0.0.1:9223"
            assert kwargs["connect_timeout_ms"] == 45_000

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def doctor(self, **kwargs):
            assert kwargs["group_name"] == "fixture"
            return BrowserDoctorResult(True, "ok", "ready")

    monkeypatch.setattr(module, "BrowserSession", FakeSession)
    monkeypatch.setattr(sys, "argv", _argv(config))

    assert module.main() == 0
    assert '"reason_code": "ready"' in capsys.readouterr().out


def test_preflight_preserves_login_and_browser_blocked_diagnoses(tmp_path, monkeypatch, capsys):
    config = tmp_path / "job.json"
    config.write_text("{}", encoding="utf-8")
    module = _module()

    class LoginSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def doctor(self, **_kwargs):
            return BrowserDoctorResult(False, "need_reauth", "login")

    monkeypatch.setattr(module, "BrowserSession", LoginSession)
    monkeypatch.setattr(sys, "argv", _argv(config))
    assert module.main() == 20
    assert "need_reauth" in capsys.readouterr().out

    class BrokenSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            raise BrowserSessionError("blocked_browser_cdp_unresponsive", "fixture")

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(module, "BrowserSession", BrokenSession)
    monkeypatch.setattr(sys, "argv", _argv(config))
    assert module.main() == 22
    assert "blocked_browser_cdp_unresponsive" in capsys.readouterr().out

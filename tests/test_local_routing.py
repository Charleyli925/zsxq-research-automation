from pathlib import Path
import json

import pytest

from zsxq_pipeline.providers.local_routing import local_routing_arguments
from zsxq_pipeline.providers.codex import CodexProviderConfig, CodexSummaryProvider


def route(tmp_path):
    catalog = tmp_path / "model catalog.json"
    catalog.write_text('{"models": []}')
    config = tmp_path / "config.toml"
    config.write_text(
        'openai_base_url = "http://127.0.0.1:10100/v1"\n'
        f'model_catalog_json = {json.dumps(str(catalog))}\n'
        'model = "unrelated-default"\n'
        '[hooks]\ncommand = "must-not-be-inherited"\n'
        '[mcp_servers.private]\ncommand = "must-not-be-inherited"\n'
        '[shell_environment_policy.set]\nSECRET_EXAMPLE = "must-not-be-inherited"\n'
    )
    return config, catalog


def test_only_local_endpoint_and_catalog_are_forwarded(tmp_path):
    config, catalog = route(tmp_path)
    args = local_routing_arguments(config)
    assert args == ('--config', 'openai_base_url="http://127.0.0.1:10100/v1"',
                    '--config', 'model_catalog_json=' + json.dumps(str(catalog)))
    assert "must-not-be-inherited" not in str(args)
    assert local_routing_arguments(None) == ()


@pytest.mark.parametrize("change", [
    lambda text: text.replace('http://127.0.0.1:10100/v1', 'http://user:password@localhost/v1'),
    lambda text: text.replace('http://127.0.0.1:10100/v1', 'https://example.com/v1?key=secret'),
    lambda text: 'model_provider = "another"\n' + text,
    lambda text: text.replace('model_catalog_json =', 'missing_catalog ='),
])
def test_invalid_config_does_not_fall_back_to_direct_upstream(tmp_path, change):
    config, _ = route(tmp_path)
    config.write_text(change(config.read_text()))
    with pytest.raises(ValueError):
        local_routing_arguments(config)


def test_missing_catalog_or_config_is_an_error(tmp_path):
    config, catalog = route(tmp_path)
    catalog.unlink()
    with pytest.raises(ValueError):
        local_routing_arguments(config)
    with pytest.raises(FileNotFoundError):
        local_routing_arguments(tmp_path / "missing.toml")


def test_summary_uses_the_route_while_keeping_model_and_isolation(tmp_path):
    config, _ = route(tmp_path)
    provider = CodexSummaryProvider(CodexProviderConfig(
        model="gpt-6.1-sol", reasoning="high", work_root=tmp_path, routing_config_path=config))
    args = provider._command_for(tmp_path)
    assert args[args.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in args
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert 'openai_base_url="http://127.0.0.1:10100/v1"' in args
    assert args[-1] == "-"


def test_daily_digest_passes_the_same_routing_override(tmp_path, monkeypatch):
    from dataclasses import replace
    import subprocess
    from zsxq_pipeline.config import load_pipeline_config
    from zsxq_pipeline.daily_digest import CodexDigestModel, VERIFY_SCHEMA

    routing, catalog = route(tmp_path)
    pipeline = tmp_path / "pipeline.toml"
    pipeline.write_text(f'''[runtime]
root = {json.dumps(str(tmp_path))}
database = "state.sqlite"
[codex]
model = "gpt-6.1-sol"
reasoning = "high"
prompt_version = "test"
routing_config_path = {json.dumps(str(routing))}
''')
    config = load_pipeline_config(pipeline)
    config = replace(config, daily_digest=replace(config.daily_digest, enabled=True))
    captured = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured.extend(args)
            self.output = Path(args[args.index("--output-last-message") + 1])

        def communicate(self, prompt, timeout):
            self.output.write_text('{"accepted":true,"reason":"verified"}')

    monkeypatch.setattr(subprocess, "Popen", Process)
    CodexDigestModel(config)(tmp_path, "verify", "check", VERIFY_SCHEMA, (catalog,))
    assert captured[captured.index("--model") + 1] == "gpt-6.1-sol"
    assert 'openai_base_url="http://127.0.0.1:10100/v1"' in captured
    assert 'model_catalog_json=' + json.dumps(str(catalog)) in captured

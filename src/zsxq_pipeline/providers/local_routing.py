"""Explicitly reuse local Codex routing without inheriting unrelated settings."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from urllib.parse import urlsplit


def local_routing_arguments(config_path: Path | None) -> tuple[str, ...]:
    """Allowlist endpoint/catalog only; never copy credentials, hooks or MCPs.

    A configured route must be complete and readable. Missing routing is an
    error, not permission to silently send a job to the default upstream.
    """
    if config_path is None:
        return ()
    with Path(config_path).open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("model_provider", "openai") != "openai":
        raise ValueError("local routing currently requires the built-in openai provider")
    endpoint = config.get("openai_base_url")
    catalog = config.get("model_catalog_json")
    if not isinstance(endpoint, str) or not isinstance(catalog, str):
        raise ValueError("routing config requires openai_base_url and model_catalog_json")
    url = urlsplit(endpoint)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("routing endpoint must be an HTTP(S) URL without credentials, query or fragment")
    catalog_path = Path(catalog).expanduser()
    if not catalog_path.is_absolute() or not catalog_path.is_file():
        raise ValueError("routing model_catalog_json must be an existing absolute file")
    return ("--config", "openai_base_url=" + json.dumps(endpoint),
            "--config", "model_catalog_json=" + json.dumps(str(catalog_path)))

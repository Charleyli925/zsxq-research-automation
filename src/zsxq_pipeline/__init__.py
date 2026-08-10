"""The dependency-free state core for the ZSXQ research pipeline.

This package deliberately has no import-time connection to browser automation,
model runtimes, OpenClaw, or Feishu.  Those integrations are added by later
pipeline stages through the public state API.
"""

from __future__ import annotations

__version__ = "0.1.0"
SCHEMA_VERSION = 2

from .model import SummaryIdentity

__all__ = ["SCHEMA_VERSION", "SummaryIdentity", "__version__"]

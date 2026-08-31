"""Environment policy for child processes that never need provider credentials."""

from __future__ import annotations

import os
import re

_SENSITIVE_NAME = re.compile(
    r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)",
    flags=re.IGNORECASE,
)
_EXPLICIT = {
    "OPENCODE_GO_API_KEY",
    "MX_APIKEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}


def safe_subprocess_env() -> dict[str, str]:
    """Copy the process environment while removing credential-like variables."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _EXPLICIT and not _SENSITIVE_NAME.search(name)
    }

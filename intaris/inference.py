"""Cognis-managed inference configuration and lazy service credential loading."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit


def managed_endpoint() -> str | None:
    """Validate explicit managed mode without requiring Cognis to be running."""
    url = os.environ.get("COGNIS_INFERENCE_URL", "").rstrip("/")
    path = os.environ.get("COGNIS_INFERENCE_TOKEN_FILE", "")
    if not url and not path:
        return None
    parsed = urlsplit(url)
    if (
        not path
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Managed inference requires COGNIS_INFERENCE_URL and COGNIS_INFERENCE_TOKEN_FILE"
        )
    return url


def token_supplier() -> Callable[[], str]:
    """Read each request's token so startup and credential rotation are independent."""
    path = Path(os.environ["COGNIS_INFERENCE_TOKEN_FILE"])

    def read_token() -> str:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise RuntimeError(
                "Cognis inference credential is not available yet"
            ) from None
        if not token:
            raise RuntimeError("Cognis inference credential is empty")
        return token

    return read_token

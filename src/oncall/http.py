"""The single HTTP entry point for every tool.

It refuses any request to the feature-flag service: the rules forbid reading
flag settings (flagd files, flag UI or flag API) to diagnose an incident.
"""

from __future__ import annotations

from typing import Any

import httpx

FORBIDDEN_MARKERS = ("flagd", ":8013", ":8016", "/feature", "flagd-ui")


class ForbiddenEndpoint(RuntimeError):
    pass


def check_allowed(url: str) -> None:
    lowered = url.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in lowered:
            raise ForbiddenEndpoint(f"Refusing to call {url}: feature-flag settings are off limits")


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
    check_allowed(url)
    response = httpx.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def post_json(url: str, body: dict[str, Any], timeout: float = 120) -> Any:
    check_allowed(url)
    response = httpx.post(url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()

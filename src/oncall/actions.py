"""Client for the Action API. This is the agent's only way to change the system."""

from __future__ import annotations

import os
from typing import Any

import httpx

from oncall.schema import ProposedAction

URL = os.environ.get("ACTION_API_URL", "http://127.0.0.1:8765")


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    # The API answers 403/500 with a JSON log entry; return it either way.
    response = httpx.post(f"{URL}{path}", json=body, timeout=180)
    return response.json()


def execute(proposal: ProposedAction, approved_by: str | None, dry_run: bool, incident_id: str) -> dict[str, Any]:
    return _post("/actions", {
        "action": proposal.action,
        "target": proposal.target,
        "params": proposal.params,
        "reason": proposal.reason,
        "approved_by": approved_by,
        "dry_run": dry_run,
        "incident_id": incident_id,
    })


def rollback(action_id: str, approved_by: str | None) -> dict[str, Any]:
    return _post(f"/actions/{action_id}/rollback", {"approved_by": approved_by})


def available() -> bool:
    try:
        return httpx.get(f"{URL}/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False

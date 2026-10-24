"""Validation, execution, rollback and the call log for allowlisted actions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ops import docker, flags, loadgen

LOG_FILE = Path(os.environ.get("ACTION_LOG", "runs/actions.jsonl"))
KILL_SWITCH = Path(os.environ.get("KILL_SWITCH_FILE", ".killswitch"))

# The agent must never take down its own eyes or touch the flag service directly.
PROTECTED = {"otel-collector", "jaeger", "prometheus", "opensearch", "grafana", "flagd", "flagd-ui", "opamp-server"}


class ActionError(ValueError):
    """The request is not allowed or not valid; nothing was changed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Each handler validates, then (unless dry_run) acts and returns (result, undo info).
def _restart(target: str, params: dict, dry_run: bool) -> tuple[str, dict]:
    if target in PROTECTED:
        raise ActionError(f"{target} is part of the observability/flag stack and cannot be restarted by the agent")
    if target not in docker.demo_containers():
        raise ActionError(f"{target!r} is not a demo container")
    if dry_run:
        return f"would restart {target}", {}
    if docker.is_running(target):
        docker.restart(target)
        return f"restarted {target}", {}
    docker.start(target)
    return f"started stopped container {target}", {}


def _toggle_flag(target: str, params: dict, dry_run: bool) -> tuple[str, dict]:
    variant = str(params.get("variant", "off"))
    if dry_run:
        return f"would set flag {target} to {variant}", {}
    try:
        previous = flags.set_variant(target, variant)
    except flags.FlagError as exc:
        raise ActionError(str(exc)) from exc
    return f"set flag {target} to {variant}", {"previous_variant": previous}


def _throttle(target: str, params: dict, dry_run: bool) -> tuple[str, dict]:
    users = int(params.get("users", 1))
    if users < 0:
        raise ActionError("users must be >= 0")
    if dry_run:
        return f"would set load generator to {users} users", {}
    previous = loadgen.user_count()
    loadgen.set_users(users)
    return f"load generator {previous} -> {users} users", {"previous_users": previous}


def _scale(target: str, params: dict, dry_run: bool) -> tuple[str, dict]:
    raise ActionError("scale_service needs Kubernetes; this demo runs on Docker Compose")


HANDLERS: dict[str, Callable[[str, dict, bool], tuple[str, dict]]] = {
    "restart_service": _restart,
    "toggle_feature_flag": _toggle_flag,
    "throttle_load_generator": _throttle,
    "scale_service": _scale,
}


def _append(entry: dict[str, Any]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _archives() -> list[Path]:
    return sorted(LOG_FILE.parent.glob(f"{LOG_FILE.stem}-*{LOG_FILE.suffix}"))


def history(archived: bool = False) -> list[dict[str, Any]]:
    """The current log; with `archived`, also every log set aside by clear(), oldest first."""
    files = (_archives() if archived else []) + [LOG_FILE]
    return [json.loads(line) for f in files if f.exists() for line in f.read_text().splitlines() if line.strip()]


def clear() -> str | None:
    """Start a fresh log. The old one is kept as an archive (it is the audit record), never deleted."""
    if not LOG_FILE.exists():
        return None
    archive = LOG_FILE.with_name(f"{LOG_FILE.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}{LOG_FILE.suffix}")
    LOG_FILE.rename(archive)
    return archive.name


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Run one action request. Every call is logged, including refused ones."""
    entry: dict[str, Any] = {
        "id": f"act-{uuid.uuid4().hex[:8]}",
        "at": _now(),
        "action": request.get("action"),
        "target": request.get("target", ""),
        "params": request.get("params") or {},
        "reason": request.get("reason", ""),
        "incident_id": request.get("incident_id"),
        "approved_by": request.get("approved_by"),
        "dry_run": bool(request.get("dry_run", True)),
    }
    try:
        handler = HANDLERS.get(entry["action"])
        if handler is None:
            raise ActionError(f"{entry['action']!r} is not an allowlisted action: {sorted(HANDLERS)}")
        if not entry["target"]:
            raise ActionError("target is required")
        if not entry["dry_run"]:
            if KILL_SWITCH.exists():
                raise ActionError("kill switch is engaged")
            if not entry["approved_by"]:
                raise ActionError("a human approver is required for a real action")
        entry["result"], entry["undo"] = handler(entry["target"], entry["params"], entry["dry_run"])
        entry["status"] = "dry_run" if entry["dry_run"] else "done"
    except ActionError as exc:
        entry.update(status="refused", result=str(exc))
    except Exception as exc:  # docker/http failures: logged, reported, never hidden
        entry.update(status="failed", result=f"{type(exc).__name__}: {exc}")
    _append(entry)
    return entry


def rollback(action_id: str, approved_by: str | None) -> dict[str, Any]:
    original = next((e for e in history(archived=True) if e["id"] == action_id), None)
    entry: dict[str, Any] = {"id": f"act-{uuid.uuid4().hex[:8]}", "at": _now(), "action": "rollback",
                             "target": action_id, "approved_by": approved_by}
    try:
        if original is None or original.get("status") != "done":
            raise ActionError(f"{action_id} is not a completed action")
        if KILL_SWITCH.exists():
            raise ActionError("kill switch is engaged")
        undo = original.get("undo") or {}
        if "previous_variant" in undo:
            flags.set_variant(original["target"], undo["previous_variant"])
            entry["result"] = f"flag {original['target']} back to {undo['previous_variant']}"
        elif "previous_users" in undo:
            loadgen.set_users(undo["previous_users"])
            entry["result"] = f"load generator back to {undo['previous_users']} users"
        else:
            entry["result"] = "nothing to undo (a restart cannot be reversed)"
        entry["status"] = "done"
    except ActionError as exc:
        entry.update(status="refused", result=str(exc))
    except Exception as exc:
        entry.update(status="failed", result=f"{type(exc).__name__}: {exc}")
    _append(entry)
    return entry

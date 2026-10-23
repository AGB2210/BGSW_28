"""Docker tools: container health and recent lifecycle changes. Read-only commands only."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

from oncall.tools import tool

LIFECYCLE = {"create", "start", "restart", "die", "kill", "oom", "stop", "destroy", "update", "pause", "unpause"}


def docker(*args: str, timeout: float = 20) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _pct(value: str) -> float:
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return 0.0


@tool(
    "Health of the demo's containers: running or not, health check, restart count, last exit code, "
    "out-of-memory kills, and current CPU and memory use against their limits. Problems are listed first.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "One container name, or empty for all"}},
    },
)
def container_health(name: str = "") -> dict[str, Any]:
    names = [name] if name else docker("ps", "-a", "--format", "{{.Names}}").split()
    if not names:
        return {"containers": []}
    inspected = json.loads(docker("inspect", *names))
    running = [c["Name"].lstrip("/") for c in inspected if c["State"]["Running"]]
    stats = {s["Name"]: s for s in _json_lines(docker("stats", "--no-stream", "--format", "json", *running))} if running else {}

    rows = []
    for c in inspected:
        n, st = c["Name"].lstrip("/"), c["State"]
        s = stats.get(n, {})
        row = {
            "name": n,
            "state": st["Status"],
            "health": (st.get("Health") or {}).get("Status", "none"),
            "restarts": c.get("RestartCount", 0),
            "exit_code": st.get("ExitCode", 0),
            "oom_killed": st.get("OOMKilled", False),
            "started_at": st.get("StartedAt", "")[:19],
            "cpu_pct": _pct(s.get("CPUPerc", "0%")),
            "mem": s.get("MemUsage", ""),
            "mem_pct": _pct(s.get("MemPerc", "0%")),
        }
        row["problems"] = [p for p, bad in [
            ("not running", st["Status"] != "running"),
            ("unhealthy", row["health"] == "unhealthy"),
            ("restarted", row["restarts"] > 0),
            ("oom killed", row["oom_killed"]),
            ("memory near limit", row["mem_pct"] >= 97),  # JVM services idle at 92-96%
            ("high cpu", row["cpu_pct"] >= 90),
        ] if bad]
        rows.append(row)

    rows.sort(key=lambda r: (-len(r["problems"]), r["name"]))
    if not name:  # keep the all-containers view short: problems in full, healthy ones as names
        healthy = [r["name"] for r in rows if not r["problems"]]
        return {"with_problems": [r for r in rows if r["problems"]], "healthy": healthy}
    return {"containers": rows}


@tool(
    "Recent lifecycle events per container (created, started, died, killed, OOM, restarted, stopped, "
    "resource limits updated) with exit codes, plus containers recreated in the window. "
    "Use it to answer: what changed just before things broke?",
    {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Look-back window", "default": 30},
            "name": {"type": "string", "description": "One container name, or empty for all"},
        },
    },
)
def deployment_history(minutes: int = 30, name: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    args = ["events", "--since", f"{int(minutes)}m", "--until", now, "--filter", "type=container", "--format", "json"]
    if name:
        args += ["--filter", f"container={name}"]
    events = []
    for e in _json_lines(docker(*args, timeout=30)):
        action = e.get("Action", "").split(":")[0]
        if action not in LIFECYCLE:
            continue
        attrs = e.get("Actor", {}).get("Attributes", {})
        events.append({
            "time": datetime.fromtimestamp(int(e["time"]), timezone.utc).strftime("%H:%M:%S"),
            "container": attrs.get("name", "?"),
            "action": action,
            **({"exit_code": attrs["exitCode"]} if "exitCode" in attrs else {}),
            **({"image": attrs["image"]} if action == "create" else {}),
        })

    by_container: dict[str, int] = {}
    for e in events:
        by_container[e["container"]] = by_container.get(e["container"], 0) + 1

    # Docker forgets events when its daemon restarts, so also report start times directly.
    names = [name] if name else docker("ps", "-a", "--format", "{{.Names}}").split()
    cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60
    started = []
    for c in json.loads(docker("inspect", *names)) if names else []:
        at = c["State"].get("StartedAt", "")[:19]
        if at and datetime.fromisoformat(at).replace(tzinfo=timezone.utc).timestamp() >= cutoff:
            started.append({"container": c["Name"].lstrip("/"), "started_at": at[11:], "image": c["Config"]["Image"]})

    return {
        "window_min": minutes,
        "event_count": len(events),
        "busiest_containers": dict(sorted(by_container.items(), key=lambda kv: -kv[1])[:8]),
        "recent_events": events[-25:],
        "started_in_window": sorted(started, key=lambda s: s["started_at"])[-15:],
    }

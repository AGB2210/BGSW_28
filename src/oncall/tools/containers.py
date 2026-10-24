"""Docker tools: container health and recent lifecycle changes. Read-only commands only."""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from oncall.tools import tool

LIFECYCLE = {"create", "start", "restart", "die", "kill", "oom", "stop", "destroy", "update", "pause", "unpause"}
RECENT_RESTART_S = 600
ALWAYS_BUSY = {"load-generator"}  # runs a headless browser; high CPU is its normal state
LOW_CPU_LIMIT = 0.25  # cores
DEMO = "label=com.docker.compose.project=opentelemetry-demo"  # leaves out short-lived helper containers
# Read-only look at a container's network queue: a delay rule there slows every reply it sends.
NETSHOOT = "nicolaka/netshoot:v0.13"


def docker(*args: str, timeout: float = 20) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _demo_names() -> list[str]:
    return docker("ps", "-a", "--filter", DEMO, "--format", "{{.Names}}").split()


def network_delay(qdisc: str) -> str | None:
    """The delay in a `tc qdisc show` line, e.g. 'qdisc netem 8001: root ... delay 2s' -> '2s'."""
    match = re.search(r"\bnetem\b.*?\bdelay\s+(\S+)", qdisc)
    return match.group(1) if match else None


def _delay_of(name: str) -> str | None:
    try:
        out = docker("run", "--rm", "--pull", "never", "--network", f"container:{name}", NETSHOOT,
                     "tc", "qdisc", "show", "dev", "eth0", timeout=15)
    except Exception:  # helper image missing or container gone: no finding rather than a failed tool
        return None
    return network_delay(out)


def _hard_problems(c: dict[str, Any]) -> list[str]:
    """Problems visible from `docker inspect` alone (cheap enough for the detector to check every cycle)."""
    st = c["State"]
    cpu_limit = (c.get("HostConfig", {}).get("NanoCpus") or 0) / 1e9
    return [p for p, bad in [
        ("not running", st["Status"] != "running"),
        ("oom killed", st.get("OOMKilled", False)),
        (f"cpu limited to {cpu_limit:.2f} cores", 0 < cpu_limit < LOW_CPU_LIMIT),
    ] if bad]


def container_alerts() -> list[dict[str, Any]]:
    """Detector input: containers that are down, OOM-killed or starved of CPU, whatever the metrics say."""
    names = _demo_names()
    if not names:
        return []
    return [{"service": c["Name"].lstrip("/"), "kind": "container", "detail": f"container {p}"}
            for c in json.loads(docker("inspect", *names)) for p in _hard_problems(c)]


def _json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _pct(value: str) -> float:
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return 0.0


@tool(
    "Health of the demo's containers: running or not, health check, restart count, last exit code, "
    "out-of-memory kills, current CPU and memory use against their limits, and any delay rule on the "
    "container's network. Problems are listed first.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "One container name, or empty for all"}},
    },
)
def container_health(name: str = "") -> dict[str, Any]:
    known = _demo_names()
    if name and name not in known:  # e.g. frontend-web: a telemetry name with no container of its own
        return {"error": f"{name} is not a container", "containers": known}
    names = [name] if name else known
    if not names:
        return {"containers": []}
    inspected = json.loads(docker("inspect", *names))
    running = [c["Name"].lstrip("/") for c in inspected if c["State"]["Running"]]
    stats = {s["Name"]: s for s in _json_lines(docker("stats", "--no-stream", "--format", "json", *running))} if running else {}
    with ThreadPoolExecutor(max_workers=8) as pool:  # ~0.5 s per container, so check them side by side
        delays = dict(zip(running, pool.map(_delay_of, running)))

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
            # A very low CPU limit starves a service while its CPU % looks low, so report the limit itself.
            "cpu_limit": (c.get("HostConfig", {}).get("NanoCpus") or 0) / 1e9 or None,
        }
        started = st.get("StartedAt", "")[:19]
        recently_started = bool(started) and (
            datetime.now(timezone.utc) - datetime.fromisoformat(started).replace(tzinfo=timezone.utc)
        ).total_seconds() < RECENT_RESTART_S
        row["problems"] = _hard_problems(c) + [p for p, bad in [
            ("unhealthy", row["health"] == "unhealthy"),
            ("restarted recently", row["restarts"] > 0 and recently_started),  # restart counts are cumulative
            # Memory is shown but not flagged: Java services normally sit at 92-98% of their cap.
            ("high cpu", row["cpu_pct"] >= 90 and n not in ALWAYS_BUSY),
            (f"network delay {delays.get(n)}", bool(delays.get(n))),
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
    known = _demo_names()
    if name and name not in known:
        return {"error": f"{name} is not a container", "containers": known}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    args = ["events", "--since", f"{int(minutes)}m", "--until", now, "--filter", "type=container",
            "--filter", DEMO, "--format", "json"]
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
    names = [name] if name else known
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

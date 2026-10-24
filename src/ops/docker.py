"""Docker helpers scoped to the demo's Compose project."""

from __future__ import annotations

import json
import subprocess
from typing import Any

PROJECT = "opentelemetry-demo"


def run(*args: str, timeout: float = 60) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=True).stdout


def demo_containers() -> list[str]:
    out = run("ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Names}}")
    return sorted(out.split())


def inspect(name: str) -> dict[str, Any]:
    return json.loads(run("inspect", name))[0]


def is_running(name: str) -> bool:
    return bool(inspect(name)["State"]["Running"])


def restart(name: str) -> None:
    run("restart", name, timeout=120)


def start(name: str) -> None:
    run("start", name, timeout=120)


def stop(name: str) -> None:
    run("stop", name, timeout=120)


def nano_cpus(name: str) -> int:
    return int(inspect(name)["HostConfig"].get("NanoCpus") or 0)


def set_cpus(name: str, cpus: float) -> None:
    """Limit a container's CPU. 0 removes the limit.

    `docker update --cpus 0` is silently ignored, so "no limit" is set as a limit of
    every available core, which never throttles.
    """
    if cpus <= 0:
        cpus = float(run("info", "--format", "{{.NCPU}}").strip() or 1)
    run("update", "--cpus", str(cpus), name)


NETSHOOT = "nicolaka/netshoot:v0.13"


def _tc(name: str, *args: str) -> None:
    """Run `tc` inside a container's network namespace via a short-lived helper container."""
    run("run", "--rm", "--network", f"container:{name}", "--cap-add", "NET_ADMIN", NETSHOOT, "tc", *args)


def add_delay(name: str, delay_ms: int) -> None:
    """Delay all traffic leaving the container (stays until clear_delay)."""
    _tc(name, "qdisc", "replace", "dev", "eth0", "root", "netem", "delay", f"{delay_ms}ms")


def clear_delay(name: str) -> None:
    try:
        _tc(name, "qdisc", "del", "dev", "eth0", "root")
    except Exception:
        pass  # already clear (e.g. the container was recreated)

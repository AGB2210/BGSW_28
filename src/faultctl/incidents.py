"""Incident catalogue and the inject/reset logic.

Every change is recorded in a state file so `reset` can undo it exactly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ops import docker, flags, loadgen

STATE_FILE = Path(os.environ.get("FAULTCTL_STATE", "runs/faultctl-state.json"))
FLAG_BACKUP = STATE_FILE.parent / "flagd-original.json"


@dataclass(frozen=True)
class Incident:
    id: str
    kind: str  # "application" or "infrastructure"
    description: str
    flag: tuple[str, str] | None = None  # (flag name, variant)
    stop: str | None = None  # container to stop
    cpus: float | None = None  # CPU limit for the target container
    delay_ms: int | None = None  # network delay on the target container's traffic
    default_target: str | None = None


CATALOGUE = {i.id: i for i in [
    # Application faults (the demo's built-in failure flags)
    Incident("payment-failure", "application", "Payment service fails half of charge requests", flag=("paymentFailure", "50%")),
    Incident("payment-unreachable", "application", "Payment service becomes unreachable", flag=("paymentUnreachable", "on")),
    Incident("cart-failure", "application", "Cart service fails intermittently", flag=("cartFailure", "25%")),
    Incident("product-catalog-failure", "application", "Product catalog fails for one product", flag=("productCatalogFailure", "on")),
    Incident("recommendation-cache-leak", "application", "Recommendation cache runs away and leaks memory", flag=("recommendationCacheFailure", "on")),
    Incident("ad-high-cpu", "application", "Ad service burns CPU", flag=("adHighCpu", "on")),
    Incident("ad-gc", "application", "Ad service runs constant full garbage collections", flag=("adManualGc", "on")),
    Incident("homepage-flood", "application", "Traffic flood on the homepage", flag=("loadGeneratorFloodHomepage", "on")),
    Incident("image-slow", "application", "Product images load slowly", flag=("imageSlowLoad", "5sec")),
    # Infrastructure faults
    Incident("service-down", "infrastructure", "A service container is stopped", stop="", default_target="checkout"),
    Incident("cache-down", "infrastructure", "The cart cache (Valkey) is stopped", stop="valkey-cart"),
    Incident("db-down", "infrastructure", "The product database (PostgreSQL) is stopped", stop="astronomy-db"),
    Incident("cpu-starve", "infrastructure", "A service is starved of CPU", cpus=0.05, default_target="product-catalog"),
    Incident("network-latency", "infrastructure", "Network latency on one service's traffic (2 s)",
             delay_ms=2000, default_target="cart"),
]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"active": []}


def _save(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def inject(incident_id: str, target: str | None = None) -> dict[str, Any]:
    incident = CATALOGUE.get(incident_id)
    if incident is None:
        raise KeyError(f"unknown incident {incident_id!r}; see `faultctl list`")
    state = load_state()
    record: dict[str, Any] = {"incident": incident_id, "at": _now(), "changes": []}

    if incident.flag:
        flags.backup(FLAG_BACKUP)
        name, variant = incident.flag
        previous = flags.set_variant(name, variant)
        record["changes"].append({"type": "flag", "flag": name, "previous": previous})
    if incident.stop is not None:
        container = incident.stop or target or incident.default_target
        docker.stop(container)
        record["changes"].append({"type": "stopped", "container": container})
    if incident.cpus is not None:
        container = target or incident.default_target
        record["changes"].append({"type": "cpus", "container": container, "previous_nano": docker.nano_cpus(container)})
        docker.set_cpus(container, incident.cpus)
    if incident.delay_ms is not None:
        container = target or incident.default_target
        docker.add_delay(container, incident.delay_ms)
        record["changes"].append({"type": "delay", "container": container})

    state["active"].append(record)
    _save(state)
    return record


def reset() -> list[str]:
    """Undo every recorded change, newest first, then restore the original flag file."""
    state = load_state()
    done = []
    for record in reversed(state["active"]):
        for change in reversed(record["changes"]):
            if change["type"] == "flag":
                flags.set_variant(change["flag"], change["previous"])
                done.append(f"flag {change['flag']} -> {change['previous']}")
            elif change["type"] == "stopped":
                docker.start(change["container"])
                done.append(f"started {change['container']}")
            elif change["type"] == "cpus":
                docker.set_cpus(change["container"], change["previous_nano"] / 1e9)
                done.append(f"cpu limit restored on {change['container']}")
            elif change["type"] == "delay":
                docker.clear_delay(change["container"])
                done.append(f"network delay removed from {change['container']}")
    if FLAG_BACKUP.exists():
        flags.restore(FLAG_BACKUP)
        FLAG_BACKUP.unlink()
        done.append("flag file restored from backup")
    _save({"active": []})
    return done


def baseline_load(users: int = loadgen.DEFAULT_USERS) -> None:
    """Put the load generator back to the demo's default load (a restart; see loadgen.restore)."""
    if loadgen.user_count() != users:
        loadgen.restore(users)

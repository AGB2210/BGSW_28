"""Choose an allowlisted remedy for a diagnosis, using fixed, explainable rules.

The model diagnoses; it does not pick the action. A remedy is proposed only
when the evidence shows something an allowlisted action can fix.
"""

from __future__ import annotations

import re

from oncall.evidence import Ledger
from oncall.schema import Diagnosis, ProposedAction

TRAFFIC_SOURCES = {"load-generator"}
# A restart brings a stopped or wedged container back.
RESTART_FIXES = ("not running", "unhealthy", "restarted", "oom killed", "memory near limit")


def _container_problems(component: str, ledger: Ledger) -> list[str]:
    """Problems container_health reported for this component, newest evidence first."""
    for e in reversed(ledger.items()):
        if e.tool != "container_health" or not isinstance(e.summary, dict):
            continue
        rows = e.summary.get("containers") or e.summary.get("with_problems") or []
        for row in rows:
            if row.get("name") == component:
                return list(row.get("problems", []))
        if component in e.summary.get("healthy", []):
            return []
    return []


def _surge(component: str, alert: str) -> str | None:
    """The alert's traffic-surge line for this component, e.g. 'request rate 74.8/s (baseline 27.9/s)'."""
    match = re.search(rf"(?:^|;\s*){re.escape(component)}: (request rate [^;]+)", alert or "")
    return match.group(1) if match else None


def _only_a_flood(alert: str, ledger: Ledger) -> bool:
    """A pure traffic flood: every alert line is a request-rate surge, and no service in the
    metrics evidence is slow. A surge that comes with slowness (e.g. slow images making clients
    retry) has another cause, and throttling would only hide it."""
    lines = [part.split(":", 1)[-1].strip() for part in (alert or "").split(";") if part.strip()]
    if not lines or not all(line.startswith("request rate") for line in lines):
        return False
    for e in ledger.items():
        if e.tool != "service_overview" or not isinstance(e.summary, list):
            continue
        for r in e.summary:
            p95, base = (r.get("p95_ms") or 0), (r.get("base_p95_ms") or 0)
            if r.get("service") not in TRAFFIC_SOURCES and p95 >= 500 and p95 >= 3 * max(base, 1):
                return False
    return True


def pure_flood(component: str | None, alert: str, ledger: Ledger) -> str | None:
    """The surge line when `component` is flooded and nothing else is wrong, else None."""
    surge = _surge(component or "", alert)
    return surge if surge and _only_a_flood(alert, ledger) else None


def propose(diagnosis: Diagnosis, ledger: Ledger, alert: str = "") -> tuple[ProposedAction | None, str]:
    """Return (action or None, explanation)."""
    if diagnosis.escalate or not diagnosis.component:
        return None, "Diagnosis is not confident enough to act on; escalating with findings."

    component = diagnosis.component
    if component in TRAFFIC_SOURCES:
        return ProposedAction(
            action="throttle_load_generator", target="load-generator", params={"users": 1},
            reason=f"Traffic flood from {component}; reduce load until the source is understood.",
        ), "Traffic flood: throttle the load generator."

    problems = _container_problems(component, ledger)
    found = ", ".join(problems)
    if any(p.startswith("cpu limited") for p in problems):
        return ProposedAction(
            action="remove_cpu_limit", target=component,
            reason=f"{component} container shows: {found}. It is starved of CPU; lifting the limit lets it serve again.",
        ), f"CPU starvation on {component}: lift the CPU limit."
    if any(p.startswith("network delay") for p in problems):
        return ProposedAction(
            action="restart_service", target=component,
            reason=f"{component} container shows: {found}. A restart rebuilds the container's network, "
                   "which drops the delay rule.",
        ), f"Network delay on {component}: restart it."
    if any(p in problems for p in RESTART_FIXES):
        return ProposedAction(
            action="restart_service", target=component,
            reason=f"{component} container shows: {found}. A restart restores a stopped or wedged service.",
        ), f"Container problem on {component}: restart it."

    surge = pure_flood(component, alert, ledger)
    if surge and not problems:
        return ProposedAction(
            action="throttle_load_generator", target="load-generator", params={"users": 1},
            reason=f"{component} is healthy but flooded ({surge}). The load generator is the only traffic "
                   "source here, so reduce it until the flood is understood.",
        ), f"Traffic surge on {component}: throttle the load generator."

    return None, (f"{component} is running and healthy at the container level, so the fault is inside the "
                  "application. No allowlisted action safely fixes that; escalating with findings.")

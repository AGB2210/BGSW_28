"""Choose an allowlisted remedy for a diagnosis, using fixed, explainable rules.

The model diagnoses; it does not pick the action. A remedy is proposed only
when the evidence shows something an allowlisted action can fix.
"""

from __future__ import annotations

from oncall.evidence import Ledger
from oncall.schema import Diagnosis, ProposedAction

TRAFFIC_SOURCES = {"load-generator"}


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


def propose(diagnosis: Diagnosis, ledger: Ledger) -> tuple[ProposedAction | None, str]:
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
    if any(p in problems for p in ("not running", "unhealthy", "restarted", "oom killed", "memory near limit")):
        return ProposedAction(
            action="restart_service", target=component,
            reason=f"{component} container shows: {', '.join(problems)}. A restart restores a stopped or wedged service.",
        ), f"Container problem on {component}: restart it."

    return None, (f"{component} is running and healthy at the container level, so the fault is inside the "
                  "application. No allowlisted action safely fixes that; escalating with findings.")

"""Alert detector: compares live service metrics with a saved healthy baseline.

It fires on an error-rate jump, a latency jump, or a service that stops serving.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oncall.tools.metrics import service_rows

BASELINE_FILE = Path("runs/baseline.json")
# The load generator only reports client-side symptoms; it is never the alerting service.
IGNORED = {"load-generator"}

ERROR_JUMP_PCT = 5.0      # error rate must rise by at least this many points ...
ERROR_MIN_PCT = 5.0       # ... and reach at least this level
LATENCY_FACTOR = 3.0      # p95 must be this many times the baseline ...
LATENCY_MIN_MS = 1000.0   # ... and at least this slow
SILENT_MIN_RPS = 0.2      # a service this busy at baseline that drops to zero is "silent"


def snapshot(window: str = "2m") -> dict[str, dict[str, Any]]:
    return {r["service"]: r for r in service_rows(window)}


def save_baseline(window: str = "5m") -> dict[str, dict[str, Any]]:
    data = snapshot(window)
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(data, indent=2))
    return data


def load_baseline() -> dict[str, dict[str, Any]]:
    return json.loads(BASELINE_FILE.read_text()) if BASELINE_FILE.exists() else {}


def anomalies(baseline: dict[str, dict], current: dict[str, dict]) -> list[dict[str, Any]]:
    found = []
    for svc in sorted(set(baseline) | set(current)):
        if svc in IGNORED:
            continue
        base, now = baseline.get(svc, {}), current.get(svc)
        if now is None:
            if base.get("rps", 0) >= SILENT_MIN_RPS:
                found.append({"service": svc, "kind": "silent", "detail": f"no requests (baseline {base['rps']}/s)"})
            continue
        base_err = base.get("error_pct", 0.0)
        if now["error_pct"] >= ERROR_MIN_PCT and now["error_pct"] - base_err >= ERROR_JUMP_PCT:
            found.append({"service": svc, "kind": "errors",
                          "detail": f"error rate {now['error_pct']}% (baseline {base_err}%)"})
        base_p95, p95 = base.get("p95_ms") or 0.0, now.get("p95_ms") or 0.0
        if p95 >= LATENCY_MIN_MS and p95 >= LATENCY_FACTOR * max(base_p95, 1.0):
            found.append({"service": svc, "kind": "latency", "detail": f"p95 {p95:.0f} ms (baseline {base_p95:.0f} ms)"})
    rank = {"errors": 0, "silent": 1, "latency": 2}
    return sorted(found, key=lambda a: rank[a["kind"]])


def alert_text(found: list[dict[str, Any]]) -> str | None:
    if not found:
        return None
    return "; ".join(f"{a['service']}: {a['detail']}" for a in found[:4])


def check(window: str = "2m") -> str | None:
    """Return an alert line, or None when everything looks like the baseline."""
    return alert_text(anomalies(load_baseline(), snapshot(window)))

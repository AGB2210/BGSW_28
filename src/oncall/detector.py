"""Alert detector: compares live service metrics with a saved healthy baseline.

It fires on an error-rate jump, a latency jump, a traffic surge, a service that stops
serving, or a container that is down, OOM-killed or starved of CPU.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from oncall.tools.containers import container_alerts
from oncall.tools.metrics import service_rows

BASELINE_FILE = Path("runs/baseline.json")
# The load generator only reports client-side symptoms; it is never the alerting service.
IGNORED = {"load-generator"}
# Browser-side telemetry comes from a single simulated browser user and drops out on its own,
# so its silence is not an incident (its errors and latency still count).
NEVER_SILENT = {"frontend-web"}

ERROR_JUMP_PCT = 5.0      # error rate must rise by at least this many points ...
ERROR_MIN_PCT = 5.0       # ... reach at least this level ...
ERROR_FACTOR = 2.0        # ... be at least this many times the baseline rate ...
ERROR_MIN_COUNT = 4       # ... and come from at least this many failed requests in the window
WINDOW_SECONDS = 180
LATENCY_FACTOR = 3.0      # p95 must be this many times the baseline ...
LATENCY_MIN_MS = 500.0    # ... and at least this slow
SURGE_FACTOR = 2.0        # request rate this many times the baseline ...
SURGE_MIN_EXTRA_RPS = 5.0 # ... and at least this many extra requests per second
SILENT_MIN_RPS = 0.3      # a service this busy at baseline that stops completely is "silent"
GAP_FRACTION = 0.5        # this share of services silent at once = missing telemetry, not an incident

# Prometheus scrapes every 60 s, so rate() needs a window of several minutes to have
# enough samples; shorter windows are often empty and look like a silent service.
WINDOW = "3m"


class MetricsUnavailable(RuntimeError):
    """Prometheus did not answer (e.g. the host is overloaded during an incident)."""


def snapshot(window: str = WINDOW) -> dict[str, dict[str, Any]]:
    for attempt in range(2):
        try:
            return {r["service"]: r for r in service_rows(window)}
        except Exception as exc:  # timeouts or connection errors: retry once, then report clearly
            if attempt:
                raise MetricsUnavailable(f"metrics backend did not answer: {type(exc).__name__}") from exc
            time.sleep(5)
    return {}


def save_baseline(window: str = "5m") -> dict[str, dict[str, Any]]:
    data = snapshot(window)
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(data, indent=2))
    return data


def load_baseline() -> dict[str, dict[str, Any]]:
    return json.loads(BASELINE_FILE.read_text()) if BASELINE_FILE.exists() else {}


def anomalies(baseline: dict[str, dict], current: dict[str, dict],
              recent: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """Compare current metrics with the baseline.

    `recent` is a longer-window snapshot used to confirm that a missing service is
    really silent (defaults to `current`).
    """
    recent = current if recent is None else recent
    busy = [s for s, b in baseline.items() if s not in IGNORED | NEVER_SILENT and b.get("rps", 0) >= SILENT_MIN_RPS]
    silent = [s for s in busy if s not in recent]
    telemetry_gap = bool(busy) and len(silent) / len(busy) >= GAP_FRACTION

    found = []
    for svc in sorted(set(baseline) | set(current)):
        if svc in IGNORED:
            continue
        base, now = baseline.get(svc, {}), current.get(svc)
        if now is None:
            if svc in silent and not telemetry_gap:
                found.append({"service": svc, "kind": "silent", "detail": f"no requests (baseline {base['rps']}/s)"})
            continue
        base_err = base.get("error_pct", 0.0)
        failed = now["rps"] * now["error_pct"] / 100 * WINDOW_SECONDS
        if (now["error_pct"] >= ERROR_MIN_PCT and now["error_pct"] - base_err >= ERROR_JUMP_PCT
                and now["error_pct"] >= ERROR_FACTOR * base_err and failed >= ERROR_MIN_COUNT):
            found.append({"service": svc, "kind": "errors",
                          "detail": f"error rate {now['error_pct']}% (baseline {base_err}%)"})
        base_p95, p95 = base.get("p95_ms") or 0.0, now.get("p95_ms") or 0.0
        if p95 >= LATENCY_MIN_MS and p95 >= LATENCY_FACTOR * max(base_p95, 1.0):
            found.append({"service": svc, "kind": "latency", "detail": f"p95 {p95:.0f} ms (baseline {base_p95:.0f} ms)"})
        base_rps = base.get("rps", 0.0)
        if base_rps and now["rps"] >= SURGE_FACTOR * base_rps and now["rps"] - base_rps >= SURGE_MIN_EXTRA_RPS:
            found.append({"service": svc, "kind": "surge",
                          "detail": f"request rate {now['rps']:.1f}/s (baseline {base_rps:.1f}/s)"})
    return _ranked(found)


def _ranked(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"container": 0, "errors": 1, "silent": 2, "latency": 3, "surge": 4}
    return sorted(found, key=lambda a: rank[a["kind"]])


def alert_text(found: list[dict[str, Any]]) -> str | None:
    if not found:
        return None
    return "; ".join(f"{a['service']}: {a['detail']}" for a in found[:4])


def _containers() -> list[dict[str, Any]]:
    try:
        return [a for a in container_alerts() if a["service"] not in IGNORED]
    except Exception:  # Docker briefly unreachable: metrics alone still work
        return []


def current_anomalies() -> list[dict[str, Any]]:
    return _ranked(_containers() + anomalies(load_baseline(), snapshot(WINDOW)))


def check() -> str | None:
    """Return an alert line, or None when everything looks like the baseline."""
    return alert_text(current_anomalies())

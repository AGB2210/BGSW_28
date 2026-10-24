"""Prometheus tools. Span metrics come from the OTel Collector's spanmetrics connector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oncall.config import get_settings
from oncall.http import get_json
from oncall.tools import tool

HIDDEN_SERVICES = {"flagd", "flagd-ui"}


def promql(query: str) -> list[dict[str, Any]]:
    """Run an instant PromQL query and return the raw result vector."""
    url = f"{get_settings().prometheus_url}/api/v1/query"
    return get_json(url, params={"query": query}, timeout=30)["data"]["result"]


def _by_service(query: str) -> dict[str, float]:
    return {r["metric"].get("service_name", "?"): float(r["value"][1]) for r in promql(query)}


@tool(
    "Request rate, error rate and p95 latency for every service over a recent window. "
    "Call this first to see which services look unhealthy.",
    {
        "type": "object",
        "properties": {"window": {"type": "string",
                                  "description": "PromQL range; 3m matches the alert detector", "default": "3m"}},
    },
)
def service_overview(window: str = "3m") -> list[dict[str, Any]]:
    rows = service_rows(window)[:12]
    baseline = _baseline()
    for r in rows:  # show what "normal" looks like next to each service
        b = baseline.get(r["service"])
        if b:
            r["base_rps"], r["base_p95_ms"] = b.get("rps"), b.get("p95_ms")
    return rows


BASELINE_FILE = Path("runs/baseline.json")


def _baseline() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError):
        return {}


# Calls to the flag service are excluded: its long-lived event streams reconnect whenever a
# flag changes and would show up as 15 s "requests" (noise, and a side channel on flag changes).
NOT_FLAGD = 'span_name!~"flagd\\\\..*"'


def service_rows(window: str = "5m") -> list[dict[str, Any]]:
    """All services with traffic, worst first."""
    calls = "traces_span_metrics_calls_total"
    rps = _by_service(f"sum by (service_name) (rate({calls}{{{NOT_FLAGD}}}[{window}]))")
    errors = _by_service(
        f'sum by (service_name) (rate({calls}{{status_code="STATUS_CODE_ERROR",{NOT_FLAGD}}}[{window}]))')
    p95 = _by_service(
        "histogram_quantile(0.95, sum by (le, service_name) "
        f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{NOT_FLAGD}}}[{window}])))"
    )
    rows = [
        {
            "service": svc,
            "rps": round(rate, 3),
            "error_pct": round(100 * errors.get(svc, 0.0) / rate, 1) if rate else 0.0,
            "p95_ms": round(p95[svc], 1) if svc in p95 and p95[svc] == p95[svc] else None,
        }
        for svc, rate in rps.items()
        if rate > 0 and svc not in HIDDEN_SERVICES
    ]
    return sorted(rows, key=lambda r: (-r["error_pct"], -(r["p95_ms"] or 0)))

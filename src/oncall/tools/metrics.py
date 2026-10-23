"""Prometheus tools. Span metrics come from the OTel Collector's spanmetrics connector."""

from __future__ import annotations

from typing import Any

from oncall.config import get_settings
from oncall.http import get_json
from oncall.tools import tool

HIDDEN_SERVICES = {"flagd", "flagd-ui"}


def promql(query: str) -> list[dict[str, Any]]:
    """Run an instant PromQL query and return the raw result vector."""
    url = f"{get_settings().prometheus_url}/api/v1/query"
    return get_json(url, params={"query": query})["data"]["result"]


def _by_service(query: str) -> dict[str, float]:
    return {r["metric"].get("service_name", "?"): float(r["value"][1]) for r in promql(query)}


@tool(
    "Request rate, error rate and p95 latency for every service over a recent window. "
    "Call this first to see which services look unhealthy.",
    {
        "type": "object",
        "properties": {"window": {"type": "string", "description": "PromQL range, e.g. 5m", "default": "5m"}},
    },
)
def service_overview(window: str = "5m") -> list[dict[str, Any]]:
    return service_rows(window)[:12]


def service_rows(window: str = "5m") -> list[dict[str, Any]]:
    """All services with traffic, worst first."""
    calls = "traces_span_metrics_calls_total"
    rps = _by_service(f"sum by (service_name) (rate({calls}[{window}]))")
    errors = _by_service(f'sum by (service_name) (rate({calls}{{status_code="STATUS_CODE_ERROR"}}[{window}]))')
    p95 = _by_service(
        "histogram_quantile(0.95, sum by (le, service_name) "
        f"(rate(traces_span_metrics_duration_milliseconds_bucket[{window}])))"
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

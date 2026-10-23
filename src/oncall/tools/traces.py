"""Jaeger tools (Jaeger v2 query API v3, OTLP JSON).

Feature-flag data is stripped from every span before the model sees it:
spans carry `feature_flag.*` events that would reveal the flag settings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from oncall.config import get_settings
from oncall.http import get_json
from oncall.tools import tool

STATUS_ERROR = 2
HIDDEN_SERVICES = {"flagd", "flagd-ui"}
KIND = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _value(v: dict[str, Any]) -> Any:
    return next(iter(v.values()), None) if v else None


def _attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {a["key"]: _value(a.get("value", {})) for a in items if not a["key"].startswith("feature_flag")}


def fetch_spans(service: str, minutes: int, depth: int = 20) -> list[dict[str, Any]]:
    """Flattened spans from recent traces that touch `service`, flag data removed."""
    now = datetime.now(timezone.utc)
    params = {
        "query.service_name": service,
        "query.start_time_min": _ts(now - timedelta(minutes=minutes)),
        "query.start_time_max": _ts(now),
        "query.search_depth": str(depth),
    }
    try:
        data = get_json(f"{get_settings().jaeger_url}/traces", params=params, timeout=30)
    except Exception as exc:  # Jaeger v3 answers 404 when nothing matches
        if "404" in str(exc):
            return []
        raise
    spans = []
    for rs in data.get("result", {}).get("resourceSpans", []):
        svc = _attrs(rs.get("resource", {}).get("attributes", [])).get("service.name", "?")
        if svc in HIDDEN_SERVICES:
            continue
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                events = [e for e in sp.get("events", []) if not e["name"].startswith("feature_flag")]
                exc = next((_attrs(e.get("attributes", [])) for e in events if e["name"] == "exception"), {})
                attrs = _attrs(sp.get("attributes", []))
                callee = (attrs.get("rpc.service") or attrs.get("peer.service") or attrs.get("server.address")
                          or attrs.get("net.peer.name") or attrs.get("db.system") or "")
                spans.append({
                    "trace_id": sp["traceId"],
                    "span_id": sp["spanId"],
                    "parent_id": sp.get("parentSpanId", ""),
                    "service": svc,
                    "name": sp["name"],
                    "kind": KIND.get(sp.get("kind"), "?"),
                    "callee": str(callee).removeprefix("oteldemo."),
                    "error": sp.get("status", {}).get("code") == STATUS_ERROR,
                    "message": sp.get("status", {}).get("message") or exc.get("exception.message", ""),
                    "ms": (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6,
                })
    return spans


def _origins(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Error spans with no failing child: where each failure started."""
    failing_parents = {s["parent_id"] for s in spans if s["error"]}
    return [s for s in spans if s["error"] and s["span_id"] not in failing_parents]


@tool(
    "Search recent traces that involve a service. Returns how many traces failed, where each failure "
    "STARTED (the deepest failing call, usually the root cause rather than a symptom) with its error "
    "message and an example trace ID, and the slowest operations.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name, e.g. checkout"},
            "minutes": {"type": "integer", "description": "Look-back window", "default": 5},
        },
        "required": ["service"],
    },
)
def search_traces(service: str, minutes: int = 5) -> dict[str, Any]:
    if service in HIDDEN_SERVICES:
        return {"error": "flag service traces are not available to the agent"}
    spans = fetch_spans(service, minutes)
    if not spans:
        return {"service": service, "traces": 0, "note": "no traces in this window"}

    traces = {s["trace_id"] for s in spans}
    failed = {s["trace_id"] for s in spans if s["error"]}
    has_child = {s["parent_id"] for s in spans}
    groups: dict[tuple[str, str, str, str, bool], list[str]] = defaultdict(list)
    for s in _origins(spans):
        # A failed outgoing call with no span from the other side means the callee never answered.
        no_answer = s["kind"] == "client" and s["span_id"] not in has_child
        groups[(s["service"], s["name"], s["message"][:200], s["callee"], no_answer)].append(s["trace_id"])
    origins = []
    for (svc, op, msg, callee, no_answer), ids in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
        origin = {"service": svc, "operation": op, "error": msg or "(no message)", "count": len(ids), "example_trace": ids[0]}
        if callee:
            origin["calls"] = callee
        if no_answer:
            origin["note"] = f"no response recorded from {callee or 'the callee'}; suspect the callee, not {svc}"
        origins.append(origin)

    durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in spans:
        durations[(s["service"], s["name"])].append(s["ms"])
    slowest = sorted(
        ({"service": k[0], "operation": k[1], "max_ms": round(max(v), 1), "avg_ms": round(sum(v) / len(v), 1)}
         for k, v in durations.items()),
        key=lambda r: -r["max_ms"],
    )[:5]

    return {
        "service": service,
        "window_min": minutes,
        "traces": len(traces),
        "failed_traces": len(failed),
        "failure_origins": origins,
        "services_with_errors": dict(Counter(s["service"] for s in spans if s["error"]).most_common(6)),
        "slowest_operations": slowest,
    }

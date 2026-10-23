"""OpenSearch log tools. Logs from the flag service are excluded."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from oncall.config import get_settings
from oncall.http import post_json
from oncall.tools import tool

SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
HIDDEN_SERVICES = ["flagd", "flagd-ui"]
_VARIABLE = re.compile(r"\b[0-9a-f]{8,}\b|\b[0-9a-f-]{36}\b|\d+(\.\d+)?", re.IGNORECASE)


def _pattern(body: str) -> str:
    """Collapse IDs and numbers so repeats of one message group together."""
    return _VARIABLE.sub("#", body)[:160]


@tool(
    "Search recent log lines at or above a severity, grouped into distinct messages with counts. "
    "Leave service empty to see which services are logging errors.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name, or empty for all services"},
            "min_severity": {"type": "string", "enum": list(SEVERITY), "default": "WARN"},
            "contains": {"type": "string", "description": "Optional text the log line must contain"},
            "minutes": {"type": "integer", "default": 10},
        },
    },
)
def search_logs(service: str = "", min_severity: str = "WARN", contains: str = "", minutes: int = 10) -> dict[str, Any]:
    url = get_settings().opensearch_url
    if not url:
        return {"error": "OpenSearch not found; is the demo running?"}
    if service in HIDDEN_SERVICES:
        return {"error": "flag service logs are not available to the agent"}

    filters: list[dict[str, Any]] = [
        {"range": {"@timestamp": {"gte": f"now-{int(minutes)}m"}}},
        {"range": {"severity.number": {"gte": SEVERITY.get(min_severity.upper(), 17)}}},
    ]
    if service:
        filters.append({"match_phrase": {"resource.service.name": service}})
    if contains:
        filters.append({"match_phrase": {"body": contains}})
    query = {
        "size": 300,
        "sort": [{"@timestamp": "desc"}],
        "_source": ["@timestamp", "body", "severity", "resource.service.name"],
        "query": {"bool": {
            "filter": filters,
            "must_not": [{"match_phrase": {"resource.service.name": s}} for s in HIDDEN_SERVICES]
                        + [{"match_phrase": {"body": "feature_flag"}}],
        }},
    }
    hits = post_json(f"{url}/otel-logs-*/_search", query, timeout=30)["hits"]

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0})
    for h in hits["hits"]:
        src = h["_source"]
        svc = (src.get("resource") or {}).get("service.name") or (src.get("resource") or {}).get("service", {}).get("name", "?")
        body = str(src.get("body", "")).strip()
        g = groups[(svc, _pattern(body))]
        g["count"] += 1
        g.setdefault("service", svc)
        g.setdefault("severity", (src.get("severity") or {}).get("text", ""))
        g.setdefault("last_seen", src.get("@timestamp"))
        g.setdefault("example", body[:300])

    messages = sorted(groups.values(), key=lambda g: -g["count"])[:8]
    return {
        "service": service or "(all)",
        "window_min": minutes,
        "matching_lines": hits["total"]["value"],
        "distinct_messages": messages,
    }

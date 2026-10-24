#!/usr/bin/env python3
"""
knowledge_base.py - Structured operational knowledge base for the OpenTelemetry
Demo (Astronomy Shop) microservices architecture.

This module provides agents with queryable architectural knowledge:
- Service dependency graph (who calls whom)
- Service metadata (language, role, ports, data stores)
- Common failure signatures and diagnostic playbooks
- Symptom-to-root-cause mapping patterns

Any agent (detection, diagnosis, dev, orchestrator) can import and query this
knowledge base to make better-informed decisions WITHOUT needing access to
internal fault injection configurations.
"""

from typing import List, Dict, Optional, Set


# ─────────────────────────────────────────────────────────────────────
# SERVICE CATALOG
# ─────────────────────────────────────────────────────────────────────

SERVICE_CATALOG: Dict[str, dict] = {
    "frontend": {
        "language": "TypeScript/Next.js",
        "role": "Web UI — serves the Astronomy Shop storefront",
        "port": 8080,
        "calls": ["ad", "cart", "checkout", "product-catalog", "recommendation", "shipping", "currency"],
        "called_by": ["frontend-proxy", "load-generator"],
        "data_stores": [],
        "failure_impact": "Users cannot access the shop at all",
    },
    "frontend-proxy": {
        "language": "Envoy",
        "role": "Reverse proxy / API gateway — routes all external traffic to frontend",
        "port": 8080,
        "calls": ["frontend"],
        "called_by": ["load-generator"],
        "data_stores": [],
        "failure_impact": "All inbound requests fail (but proxy itself is rarely the root cause — look downstream)",
        "notes": "Almost NEVER the root cause of errors. 504s here mean a backend service is slow/failing.",
    },
    "load-generator": {
        "language": "Python/Locust",
        "role": "Synthetic traffic generator — simulates user browse/buy flows",
        "port": 8089,
        "calls": ["frontend-proxy"],
        "called_by": [],
        "data_stores": [],
        "failure_impact": "No synthetic traffic (does not affect real users)",
        "notes": "When load-generator shows errors, the REAL problem is in a downstream service it's calling.",
    },
    "checkout": {
        "language": "Go",
        "role": "Orchestrates order placement — calls payment, cart, shipping, email",
        "port": 8080,
        "calls": ["payment", "cart", "shipping", "currency", "email", "product-catalog", "kafka"],
        "called_by": ["frontend"],
        "data_stores": [],
        "failure_impact": "Users cannot complete purchases",
    },
    "cart": {
        "language": "C# / .NET",
        "role": "Shopping cart management — stores cart contents in Valkey (Redis)",
        "port": 8080,
        "calls": ["valkey-cart"],
        "called_by": ["frontend", "checkout"],
        "data_stores": ["valkey-cart"],
        "failure_impact": "Users cannot add/view/modify cart items",
    },
    "payment": {
        "language": "JavaScript/Node.js",
        "role": "Payment processing — validates and charges payment",
        "port": 8080,
        "calls": [],
        "called_by": ["checkout"],
        "data_stores": [],
        "failure_impact": "Checkout fails at payment step",
    },
    "product-catalog": {
        "language": "Go",
        "role": "Product listing and search — reads from PostgreSQL database",
        "port": 8080,
        "calls": ["postgresql"],
        "called_by": ["frontend", "checkout", "recommendation"],
        "data_stores": ["postgresql"],
        "failure_impact": "Products cannot be listed, searched, or looked up during checkout",
    },
    "recommendation": {
        "language": "Python",
        "role": "Product recommendations — suggests related products",
        "port": 8080,
        "calls": ["product-catalog"],
        "called_by": ["frontend"],
        "data_stores": [],
        "failure_impact": "Recommendations section empty or errors on product pages",
    },
    "currency": {
        "language": "C++",
        "role": "Currency conversion — converts prices between currencies",
        "port": 8080,
        "calls": [],
        "called_by": ["frontend", "checkout"],
        "data_stores": [],
        "failure_impact": "Prices cannot be converted; checkout may fail",
    },
    "shipping": {
        "language": "Rust",
        "role": "Shipping cost calculation",
        "port": 8080,
        "calls": ["quote"],
        "called_by": ["frontend", "checkout"],
        "data_stores": [],
        "failure_impact": "Shipping quotes unavailable; checkout may fail",
    },
    "quote": {
        "language": "PHP",
        "role": "Shipping quote sub-service",
        "port": 8080,
        "calls": [],
        "called_by": ["shipping"],
        "data_stores": [],
        "failure_impact": "Shipping costs cannot be calculated",
    },
    "email": {
        "language": "Ruby",
        "role": "Order confirmation emails",
        "port": 8080,
        "calls": [],
        "called_by": ["checkout"],
        "data_stores": [],
        "failure_impact": "No order confirmation emails sent (checkout may still succeed)",
    },
    "ad": {
        "language": "Java",
        "role": "Context-based advertisement service",
        "port": 8080,
        "calls": [],
        "called_by": ["frontend"],
        "data_stores": [],
        "failure_impact": "Ads don't load on product pages (non-critical)",
    },
    "image-provider": {
        "language": "Nginx",
        "role": "Static image server for product images",
        "port": 8080,
        "calls": [],
        "called_by": ["frontend"],
        "data_stores": [],
        "failure_impact": "Product images don't load (non-critical)",
    },
    "accounting": {
        "language": "Go",
        "role": "Order accounting — consumes order events from Kafka",
        "port": 8080,
        "calls": [],
        "called_by": ["kafka"],
        "data_stores": [],
        "failure_impact": "Orders not recorded in accounting (eventual consistency issue)",
    },
    "fraud-detection": {
        "language": "Kotlin",
        "role": "Fraud detection — consumes order events from Kafka",
        "port": 8080,
        "calls": [],
        "called_by": ["kafka"],
        "data_stores": [],
        "failure_impact": "Fraud checks not performed (eventual consistency issue)",
    },
    # Infrastructure services
    "valkey-cart": {
        "language": "Valkey (Redis-compatible)",
        "role": "In-memory data store for shopping cart data",
        "port": 6379,
        "calls": [],
        "called_by": ["cart"],
        "data_stores": [],
        "failure_impact": "Cart service cannot read/write cart data — all cart operations fail",
    },
    "postgresql": {
        "language": "PostgreSQL",
        "role": "Relational database for product catalog data",
        "port": 5432,
        "calls": [],
        "called_by": ["product-catalog"],
        "data_stores": [],
        "failure_impact": "Product catalog cannot list/search products",
    },
    "kafka": {
        "language": "Apache Kafka",
        "role": "Message broker for async order event processing",
        "port": 9092,
        "calls": [],
        "called_by": ["checkout"],
        "data_stores": [],
        "failure_impact": "Order events not delivered to accounting/fraud-detection",
    },
}


# ─────────────────────────────────────────────────────────────────────
# FAILURE PATTERN KNOWLEDGE
# ─────────────────────────────────────────────────────────────────────

FAILURE_PATTERNS: List[dict] = [
    {
        "id": "timeout_downstream",
        "symptoms": ["504 Gateway Timeout", "DEADLINE_EXCEEDED", "context deadline exceeded", "latency > 5000ms"],
        "interpretation": "The service receiving the timeout is NOT the problem. "
                          "The service it CALLS is slow, hanging, or unresponsive.",
        "diagnostic_steps": [
            "Identify which service has the timeout/high latency",
            "Look up its dependencies in the service catalog",
            "Check error_logs and traces for each downstream dependency",
            "The dependency that is slow, erroring, or stopped is the root cause",
        ],
        "common_causes": ["Downstream service crashed/stopped", "Downstream service has a bug causing slow responses",
                          "Database or cache is unresponsive", "Resource exhaustion (CPU/memory) in downstream service"],
    },
    {
        "id": "connection_refused",
        "symptoms": ["connection refused", "ECONNREFUSED", "dial tcp", "no such host", "connection reset"],
        "interpretation": "The target service's container is stopped, crashed, or not listening on its port.",
        "diagnostic_steps": [
            "Check container status — is the target service running?",
            "Check container stats — is it OOM-killed (memory at limit)?",
            "Check container logs for crash/startup errors",
        ],
        "common_causes": ["Container stopped/killed", "OOM kill", "Service crash on startup", "Docker network issue"],
    },
    {
        "id": "redis_failure",
        "symptoms": ["ECONNREFUSED on port 6379", "Redis connection error", "valkey", "cart.*error",
                      "failed to get cart", "failed to add item"],
        "interpretation": "Valkey/Redis (cart data store) is unavailable. Cart service will fail on all operations.",
        "diagnostic_steps": [
            "Check if valkey-cart container is running",
            "Check cart service error logs for connection errors",
            "Check valkey-cart container logs for issues",
        ],
        "common_causes": ["valkey-cart container stopped", "Memory exhaustion", "Network issue"],
    },
    {
        "id": "payment_failure",
        "symptoms": ["payment.*error", "checkout.*failed", "charge.*failed", "payment.*timeout",
                      "checkout errors with 504"],
        "interpretation": "Payment service is failing, causing checkout to fail. "
                          "The error surfaces on checkout but the root cause is in payment.",
        "diagnostic_steps": [
            "Check payment service error logs",
            "Check payment service container status and resource usage",
            "Look at checkout error traces to see which span fails",
            "Check if the error is a timeout (payment slow) or explicit error (payment bug)",
        ],
        "common_causes": ["Payment service application bug", "Payment service resource exhaustion",
                          "Payment service container issues"],
    },
    {
        "id": "database_failure",
        "symptoms": ["postgresql", "database.*error", "product-catalog.*error", "sql",
                      "products unavailable", "failed to list products"],
        "interpretation": "PostgreSQL database is unavailable, causing product-catalog failures.",
        "diagnostic_steps": [
            "Check if postgresql container is running",
            "Check product-catalog error logs for DB connection errors",
            "Check postgresql container logs",
        ],
        "common_causes": ["PostgreSQL container stopped", "Connection pool exhausted", "Disk full"],
    },
    {
        "id": "kafka_failure",
        "symptoms": ["kafka.*error", "broker", "accounting.*error", "fraud.*error",
                      "failed to produce", "failed to consume"],
        "interpretation": "Kafka is unavailable, affecting async event processing.",
        "diagnostic_steps": [
            "Check if kafka container is running",
            "Check checkout logs for Kafka send errors",
            "Check accounting and fraud-detection logs for consume errors",
        ],
        "common_causes": ["Kafka container stopped", "Topic configuration issue", "Disk full"],
    },
    {
        "id": "resource_exhaustion",
        "symptoms": ["OOMKilled", "memory.*limit", "cpu.*throttl", "container.*restart",
                      "killed", "memory > 90%"],
        "interpretation": "A container is hitting its resource limits, causing crashes or slow performance.",
        "diagnostic_steps": [
            "Check container stats for CPU% and memory%",
            "Check if the container has been restarting (restart count)",
            "Check container logs for OOM or resource-related errors",
        ],
        "common_causes": ["Memory leak", "Traffic spike", "Container memory limit too low"],
    },
    {
        "id": "load_generator_errors",
        "symptoms": ["load-generator.*error", "load-generator high_error_rate"],
        "interpretation": "Load-generator errors are ALWAYS a symptom, never the cause. "
                          "They reflect failures in the services it's calling via frontend-proxy → frontend.",
        "diagnostic_steps": [
            "IGNORE load-generator as a suspect",
            "Check error rates across all backend services",
            "Follow the dependency chain from frontend down to find which service is failing",
        ],
        "common_causes": ["Some backend service is down or erroring"],
    },
]


# ─────────────────────────────────────────────────────────────────────
# DIAGNOSTIC PLAYBOOKS
# ─────────────────────────────────────────────────────────────────────

DIAGNOSTIC_PLAYBOOKS: Dict[str, dict] = {
    "high_error_rate": {
        "description": "A service shows elevated error rate in span_metrics or HTTP/gRPC metrics",
        "steps": [
            "1. Note ALL services with high error rates (not just the first one)",
            "2. Ignore load-generator and frontend-proxy as root causes — they're proxies",
            "3. For each affected service, check get_error_logs",
            "4. If a service has errors mentioning another service, follow that dependency",
            "5. If a service has high error rate but NO error logs, check what it CALLS — the error is downstream",
            "6. Use get_error_traces to see exactly which span in the call chain failed",
            "7. The deepest service in the chain with actual errors is the root cause",
        ],
    },
    "high_latency": {
        "description": "A service shows high p95/p99 latency or 504 timeouts",
        "steps": [
            "1. 504/DEADLINE_EXCEEDED means a DOWNSTREAM service is slow — never blame the proxy or caller",
            "2. Look up the slow service's dependencies in SERVICE_CATALOG",
            "3. Check latency (get_service_latency) for each dependency",
            "4. Check if any dependency's container is struggling (get_container_stats)",
            "5. The dependency with high latency or resource exhaustion is the root cause",
        ],
    },
    "container_down": {
        "description": "A container is stopped, paused, or restarting",
        "steps": [
            "1. Identify which service's container is down",
            "2. Check what services DEPEND on it (called_by in SERVICE_CATALOG)",
            "3. Those upstream services will also show errors — they're victims, not causes",
            "4. Check the stopped container's last logs for why it stopped",
            "5. Check container stats before death — was it OOM?",
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────
# QUERY API
# ─────────────────────────────────────────────────────────────────────

def get_service_info(service: str) -> Optional[dict]:
    """Get full catalog entry for a service."""
    return SERVICE_CATALOG.get(service)


def get_dependencies(service: str) -> List[str]:
    """What services does this service call (downstream dependencies)?"""
    svc = SERVICE_CATALOG.get(service, {})
    return svc.get("calls", [])


def get_dependents(service: str) -> List[str]:
    """What services call this service (upstream dependents)?"""
    svc = SERVICE_CATALOG.get(service, {})
    return svc.get("called_by", [])


def get_dependency_chain(service: str, depth: int = 5) -> dict:
    """
    Build the full downstream dependency tree for a service.
    Returns nested dict: {service: {dep1: {subdep: ...}, dep2: {}}}
    """
    if depth <= 0:
        return {}
    deps = get_dependencies(service)
    tree = {}
    for dep in deps:
        tree[dep] = get_dependency_chain(dep, depth - 1)
    return {service: tree}


def get_all_downstream(service: str, visited: Optional[Set[str]] = None) -> List[str]:
    """Get ALL transitive downstream dependencies (flat list, no duplicates)."""
    if visited is None:
        visited = set()
    if service in visited:
        return []
    visited.add(service)
    result = []
    for dep in get_dependencies(service):
        if dep not in visited:
            result.append(dep)
            result.extend(get_all_downstream(dep, visited))
    return result


def find_root_cause_candidates(failing_service: str) -> List[str]:
    """
    Given a service that's showing errors, return the list of services
    that could be the actual root cause (itself + all its downstream deps).
    Ordered from deepest dependency to shallowest (most likely root cause first).
    """
    candidates = get_all_downstream(failing_service)
    candidates.reverse()  # deepest first
    candidates.append(failing_service)  # itself is also a candidate
    return candidates


def match_failure_patterns(symptoms: List[str]) -> List[dict]:
    """
    Given a list of symptom strings (error messages, metric descriptions),
    find matching failure patterns from the knowledge base.
    Returns matching patterns sorted by relevance (most matches first).
    """
    matches = []
    for pattern in FAILURE_PATTERNS:
        match_count = 0
        for symptom_keyword in pattern["symptoms"]:
            for s in symptoms:
                if symptom_keyword.lower() in s.lower():
                    match_count += 1
                    break
        if match_count > 0:
            matches.append({**pattern, "_match_score": match_count})

    matches.sort(key=lambda x: x["_match_score"], reverse=True)
    return matches


def get_playbook(situation: str) -> Optional[dict]:
    """Get a diagnostic playbook for a situation type."""
    return DIAGNOSTIC_PLAYBOOKS.get(situation)


def get_investigation_context(anomalies: List[dict]) -> str:
    """
    Given a list of anomaly dicts (from telemetry_collector),
    produce a structured investigation guide string that an agent can use.

    This is the main entry point for the detection agent to get knowledge-base
    context injected into its prompt.
    """
    if not anomalies:
        return "No anomalies detected. Run a broad check."

    lines = ["## KNOWLEDGE BASE CONTEXT FOR THIS INCIDENT\n"]

    # Collect affected services (skip noise like load-generator)
    affected = []
    symptoms = []
    for a in anomalies:
        svc = a.get("service", "")
        if svc and svc not in ("load-generator",):
            if svc not in affected:
                affected.append(svc)
        # Collect symptom hints
        val = a.get("value", 0)
        proto = a.get("protocol", "")
        atype = a.get("type", "")
        symptoms.append(f"{svc} {atype} {proto} value={val}")

    # For each affected service, show its deps
    lines.append("### Services with anomalies and their dependencies:")
    for svc in affected:
        deps = get_dependencies(svc)
        dep_str = ", ".join(deps) if deps else "(none — leaf service)"
        dependents = get_dependents(svc)
        dep_by_str = ", ".join(dependents) if dependents else "(none)"
        lines.append(f"- **{svc}**: calls → [{dep_str}] | called by ← [{dep_by_str}]")

    # Find root cause candidates
    lines.append("\n### Root cause candidates (check these services):")
    all_candidates = []
    for svc in affected:
        candidates = find_root_cause_candidates(svc)
        for c in candidates:
            if c not in all_candidates:
                all_candidates.append(c)
    lines.append(f"  Priority order: {', '.join(all_candidates)}")

    # Match failure patterns
    patterns = match_failure_patterns(symptoms)
    if patterns:
        lines.append("\n### Matching failure patterns:")
        for p in patterns[:3]:  # top 3
            lines.append(f"- **{p['id']}**: {p['interpretation']}")

    # Get relevant playbook
    has_timeout = any("504" in str(a) or "timeout" in str(a).lower() for a in anomalies)
    has_error_rate = any(a.get("type") == "high_error_rate" for a in anomalies)
    has_container = any("container" in str(a).lower() or "stopped" in str(a).lower() for a in anomalies)

    if has_timeout:
        pb = get_playbook("high_latency")
        if pb:
            lines.append(f"\n### Recommended playbook: {pb['description']}")
            for step in pb["steps"]:
                lines.append(f"  {step}")
    elif has_container:
        pb = get_playbook("container_down")
        if pb:
            lines.append(f"\n### Recommended playbook: {pb['description']}")
            for step in pb["steps"]:
                lines.append(f"  {step}")
    elif has_error_rate:
        pb = get_playbook("high_error_rate")
        if pb:
            lines.append(f"\n### Recommended playbook: {pb['description']}")
            for step in pb["steps"]:
                lines.append(f"  {step}")

    return "\n".join(lines)


def format_dependency_map_for_prompt() -> str:
    """
    Format the full dependency map as a compact string for injection into
    agent system prompts.
    """
    lines = ["SERVICE DEPENDENCY MAP:"]
    # Group by role
    app_services = [s for s in SERVICE_CATALOG if SERVICE_CATALOG[s].get("calls")]
    leaf_services = [s for s in SERVICE_CATALOG if not SERVICE_CATALOG[s].get("calls")
                     and s not in ("valkey-cart", "postgresql", "kafka")]
    infra_services = [s for s in ("valkey-cart", "postgresql", "kafka") if s in SERVICE_CATALOG]

    for svc in sorted(app_services):
        info = SERVICE_CATALOG[svc]
        deps = ", ".join(info["calls"])
        lines.append(f"  {svc} → [{deps}]")

    lines.append("  Leaf services (no outbound calls): " + ", ".join(sorted(leaf_services)))
    lines.append("  Data stores: " + ", ".join(infra_services))

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI TEST
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    print("=== Dependency chain for checkout ===")
    print(json.dumps(get_dependency_chain("checkout"), indent=2))

    print("\n=== Root cause candidates when checkout is failing ===")
    print(find_root_cause_candidates("checkout"))

    print("\n=== Root cause candidates when cart is failing ===")
    print(find_root_cause_candidates("cart"))

    print("\n=== Pattern match for '504 timeout' ===")
    matches = match_failure_patterns(["504 Gateway Timeout", "checkout high_error_rate"])
    for m in matches:
        print(f"  {m['id']}: {m['interpretation']}")

    print("\n=== Investigation context for sample anomalies ===")
    sample_anomalies = [
        {"service": "load-generator", "type": "high_error_rate", "protocol": "span_metrics", "value": 0.067},
        {"service": "checkout", "type": "high_error_rate", "protocol": "span_metrics", "value": 0.048},
        {"service": "cart", "type": "high_error_rate", "protocol": "span_metrics", "value": 0.002},
    ]
    print(get_investigation_context(sample_anomalies))

    print("\n=== Full dependency map ===")
    print(format_dependency_map_for_prompt())

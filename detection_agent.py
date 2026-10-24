#!/usr/bin/env python3
"""
detection_agent.py - AI Detection Agent using local Ollama LLMs.
Analyzes telemetry data to detect and diagnose faults.
Uses a ReAct (Reason-Act) loop with tool calling.

IMPORTANT: This agent diagnoses faults purely from telemetry data
           (logs, metrics, traces, container status). No internal config access.
"""

import json
import re
import sys
import datetime
from typing import Optional
from telemetry_collector import TelemetryCollector
from knowledge_base import (
    get_investigation_context,
    format_dependency_map_for_prompt,
    find_root_cause_candidates,
    match_failure_patterns,
)

# ═══════════════════════════════════════════════════════════════════════════════
# LLM Configuration
# ═══════════════════════════════════════════════════════════════════════════════

# Best models for 8GB VRAM RTX 5050:
# Detection agent needs good reasoning → qwen2.5:7b or mistral:7b
DETECTION_MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434"
MAX_ITERATIONS = 12

# ═══════════════════════════════════════════════════════════════════════════════
# Ollama Client
# ═══════════════════════════════════════════════════════════════════════════════

import requests as _requests

def ollama_chat(model: str, messages: list[dict], temperature: float = 0.1) -> str:
    """Send a chat request to Ollama and return the response text."""
    try:
        r = _requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 2048,
                "num_ctx": 8192,
            }
        }, timeout=120)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")
    except Exception as e:
        return f"[LLM ERROR] {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════════════════════════

collector = TelemetryCollector()

# Infrastructure services — the LLM must never waste steps on these
_INFRA_SERVICES = {
    "jaeger", "otel-collector", "otelcol", "prometheus", "grafana",
    "opensearch", "kafka", "valkey-cart", "valkey", "redis",
    "postgresql", "postgres", "frontend-proxy", "image-provider",
    "opamp-server", "telemetry-docs",
}

def _guard_infra(fn):
    """Wrap a tool function to block infrastructure service queries."""
    def wrapper(p):
        svc = p.get("service_name", p.get("service", "")).lower().strip()
        if svc in _INFRA_SERVICES:
            return f"[SKIP] '{svc}' is an infrastructure service. Focus on app services only."
        return fn(p)
    return wrapper


TOOLS = {
    "list_containers": {
        "desc": "List all demo containers with their status (running/stopped/paused). Call this first to see what's up.",
        "params": [],
        "fn": lambda p: collector.get_all_container_status(),
    },
    "get_container_stats": {
        "desc": "Get CPU and memory usage for a specific container. Params: service_name (e.g. 'ad', 'cart'). Only works for app services.",
        "params": ["service_name"],
        "fn": _guard_infra(lambda p: collector.get_container_stats(p["service_name"])),
    },
    "get_error_rates": {
        "desc": "Get HTTP and gRPC error rates across all services. High values indicate failures.",
        "params": [],
        "fn": lambda p: collector.get_service_error_rates(),
    },
    "get_service_latency": {
        "desc": "Get p50/p95/p99 latency for a service. Params: service (Prometheus job name e.g. 'frontend', 'checkout')",
        "params": ["service"],
        "fn": lambda p: collector.get_service_latency(p["service"]),
    },
    "get_resource_metrics": {
        "desc": "Get CPU rate and memory allocation from Prometheus for a service. Params: service",
        "params": ["service"],
        "fn": lambda p: collector.get_resource_metrics(p["service"]),
    },
    "list_metrics": {
        "desc": "Search Prometheus metric names. Params: search (keyword like 'rpc', 'http', 'cart', 'cpu')",
        "params": ["search"],
        "fn": lambda p: collector.list_metric_names(p.get("search", "")),
    },
    "query_metric": {
        "desc": "Run a raw PromQL query. Params: promql (the query string). Only use metric names from list_metrics.",
        "params": ["promql"],
        "fn": lambda p: collector.prom_query(p["promql"]),
    },
    "get_error_logs": {
        "desc": "Get ERROR/WARN log lines from a container. Params: service_name (short name like 'cart', 'payment'). Only works for app services.",
        "params": ["service_name"],
        "fn": _guard_infra(lambda p: collector.get_service_logs(p["service_name"], level="warn")),
    },
    "get_traces": {
        "desc": "Get recent Jaeger traces for a service. Params: service (the service name). Only works for app services.",
        "params": ["service"],
        "fn": _guard_infra(lambda p: collector.get_traces(p["service"], limit=5)),
    },
    "get_error_traces": {
        "desc": "Get only traces containing errors for a service. Params: service. Only works for app services.",
        "params": ["service"],
        "fn": _guard_infra(lambda p: collector.get_error_traces(p["service"], limit=5)),
    },
    "investigate_service": {
        "desc": "Deep dive: get container status, stats, logs, metrics, latency, and traces for one service all at once. Params: service. Only works for app services.",
        "params": ["service"],
        "fn": _guard_infra(lambda p: collector.collect_targeted_investigation(p["service"])),
    },
    "finish": {
        "desc": "End investigation with findings. Params: findings_summary (what the data shows), confidence (high/medium/low), affected_services (list), possible_causes (list of plausible causes), recommended_action",
        "params": ["findings_summary", "confidence", "affected_services", "possible_causes", "recommended_action"],
        "fn": None,
    },
}

TOOLS_DOC = "\n".join(
    f"  - {name}({', '.join(d['params'])}): {d['desc']}"
    for name, d in TOOLS.items()
)

# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""You are an expert SRE detection agent for the OpenTelemetry Astronomy Shop demo.
Your job is to investigate production incidents by collecting telemetry evidence — logs, metrics,
traces, and container status — and report your findings.

## CRITICAL RULES
- ALL diagnosis MUST come from telemetry data: logs, metrics, traces, container status
- You have NO access to internal service configurations — diagnose from observable signals only
- Report ONLY what the data shows. Do NOT invent or assume evidence.
- Emit exactly ONE tool call per reply as a ```json block
- NEVER call a tool that is not listed below.
- Do NOT repeat the same tool call with the same parameters.
- You do NOT need to identify the exact root cause. Report the evidence you found and
  list possible causes. A thorough evidence report with "low" confidence is MORE VALUABLE
  than a wrong diagnosis with "high" confidence.

## INVESTIGATION STRATEGY
If the alert says a container is "exited" or "stopped", that service is DOWN — call finish immediately
with high confidence. No further investigation needed.

If the initial snapshot already shows anomalies (error rates, error logs), SKIP list_containers and get_error_rates.

Step 1: Check get_error_logs for EACH service with high error rates.
        Check ALL of them, not just the first one. Don't stop early.
Step 2: For services with actual errors in logs, use get_error_traces to see the call chain.
Step 3: If traces show timeouts (504, DEADLINE_EXCEEDED), check the DIRECT dependencies
        of that service — the problem is downstream.
Step 4: Call finish with everything you found.

If no anomalies in the snapshot:
1. Start with get_error_rates, then list_containers
2. If something looks wrong, drill into it with investigate_service


## KEY INSIGHTS

### LOAD-GENERATOR IS NEVER THE ROOT CAUSE
It's a client. Its errors reflect failures in downstream services.

### TIMEOUTS POINT DOWNSTREAM
504 / DEADLINE_EXCEEDED on service X means something X CALLS is slow or failing.
- frontend-proxy is just a reverse proxy — almost NEVER the root cause
- Check DIRECT dependencies of the timeout-ing service first

### {format_dependency_map_for_prompt()}

### CONTAINER "exited" = SERVICE IS DOWN → HIGH CONFIDENCE
If the alert or list_containers shows a container with status "exited", that service is DOWN.
This is HIGH confidence — the container has stopped running. You don't need more evidence.
Immediately call finish with: confidence=high, the service as affected, and "restart the service" as the action.
Do NOT waste steps checking logs, metrics, or traces for a stopped container — it won't have any.

### "CONTAINER NOT FOUND" ≠ SERVICE DOWN
Container names in Docker may be prefixed. "Not found" might just be a naming mismatch.
Don't treat it as proof — look for actual error evidence in logs and traces.

### PRIORITIZE DIRECT DEPENDENCIES
Check direct dependencies before transitive ones. If checkout has errors, check
payment, cart, shipping BEFORE kafka or postgresql.

### EXPLICIT ERROR MESSAGES ARE STRONG EVIDENCE
If a service's logs contain an actual error message (e.g. "failed", "invalid", "connection refused",
"timeout"), that is STRONG evidence — medium or high confidence. The error is IN that service.
Don't downgrade confidence just because you haven't checked every other service.
A clear error message in a service's own logs + high error rate = medium confidence minimum.

## TOOL CALL FORMAT
```json
{{"tool": "<tool_name>", "params": {{"key": "value"}}}}
```

## FINISH TOOL GUIDANCE
When calling finish, fill in these fields based on what you ACTUALLY found:
- findings_summary: What the data shows — include actual error messages, metric values, trace patterns
- confidence: high (2+ signals confirm same cause), medium (clear error in logs), low (symptoms only)
- affected_services: Comma-separated list of services showing errors
- possible_causes: List of plausible causes. Put the most likely first.
- recommended_action: What you think should be done next based on the evidence.
  Be specific — name the service and what should happen (e.g. "investigate token
  validation in payment service", "restart cart service", "check Redis connectivity").

It is OK to have low confidence and multiple possible causes. Report honestly.
But if logs show a clear error message in a service, that IS evidence — use medium confidence.

## AVAILABLE TOOLS
{TOOLS_DOC}

## APP SERVICES (investigate these)
ad, cart, checkout, currency, email, frontend, load-generator,
payment, product-catalog, quote, recommendation, shipping,
accounting, fraud-detection

## INFRASTRUCTURE (do NOT investigate unless asked — their errors are usually internal noise)
frontend-proxy, image-provider, kafka, valkey-cart, postgresql,
otel-collector, prometheus, jaeger, grafana

Be systematic and EFFICIENT. Each tool call should gather NEW information.
Include SPECIFIC evidence in your report: metric values, log lines, trace IDs, status codes.
Focus on APP SERVICES — do not waste steps checking infrastructure services like jaeger or otel-collector.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Tool Call Parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_tool_call(text: str) -> Optional[dict]:
    """Extract a JSON tool call from the LLM response."""
    # Try json code blocks first
    for pattern in [
        r"```json\s*(\{.*?\})\s*```",
        r"```\s*(\{.*?\})\s*```",
        r'(\{[^{}]*"tool"[^{}]*\})',
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if "tool" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Detection Agent
# ═══════════════════════════════════════════════════════════════════════════════

class DetectionAgent:
    """
    ReAct agent that investigates incidents using telemetry tools.
    Produces a structured detection report.
    """

    def __init__(self, model: str = DETECTION_MODEL):
        self.model = model
        self.evidence_log = []
        self.tool_calls_log = []

    def investigate(self, alert: str, incident_id: str = "INC-001",
                    initial_snapshot: Optional[dict] = None) -> dict:
        """
        Run the investigation loop.

        Args:
            alert: Description of the alert/incident
            incident_id: Unique incident identifier
            initial_snapshot: Optional pre-collected telemetry snapshot

        Returns:
            Structured detection report (dict)
        """
        print(f"\n{'='*70}")
        print(f"  DETECTION AGENT - Investigating: {incident_id}")
        print(f"  Alert: {alert}")
        print(f"{'='*70}\n")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # If we have a pre-collected snapshot, include it
        if initial_snapshot:
            snapshot_summary = self._summarize_snapshot(initial_snapshot)
            has_anomalies = bool(initial_snapshot.get("anomalies") or
                                initial_snapshot.get("unhealthy_services"))
            if has_anomalies:
                # Get structured knowledge base context for the anomalies
                anomalies_list = initial_snapshot.get("anomalies", [])
                kb_context = get_investigation_context(anomalies_list)

                # Build list of anomalous services for specific guidance
                anomaly_services = []
                for a in anomalies_list:
                    svc = a.get("service", "")
                    if svc and svc not in anomaly_services and svc != "load-generator":
                        anomaly_services.append(svc)
                svc_list = ", ".join(anomaly_services) if anomaly_services else "the affected services"

                # Get root cause candidates — direct deps first, limited to top picks
                all_candidates = []
                for svc in anomaly_services:
                    for c in find_root_cause_candidates(svc):
                        if c not in all_candidates and c not in anomaly_services:
                            all_candidates.append(c)
                # Keep it short — top 6 candidates max so the LLM doesn't get overwhelmed
                top_candidates = all_candidates[:6]
                # The investigation order: anomalous services first, then their deps
                check_order = anomaly_services + top_candidates

                guidance = (
                    "The snapshot above shows clear anomalies. "
                    "DO NOT call list_containers or get_error_rates — that data is already above.\n\n"
                    f"{kb_context}\n\n"
                    f"INVESTIGATION PLAN: Check get_error_logs for these services IN THIS ORDER:\n"
                    f"  {', '.join(check_order)}\n"
                    "Check EACH service before concluding. Do NOT stop at the first 'container not found' — "
                    "that may just be a container naming issue, not proof of failure.\n"
                    "frontend-proxy is almost NEVER the root cause — it's just a reverse proxy."
                )
            else:
                guidance = (
                    "No anomalies in the initial snapshot. "
                    "Call get_error_rates to check for current issues, then list_containers."
                )
            messages.append({"role": "user", "content": (
                f"INCIDENT ALERT: {alert}\n\n"
                f"INITIAL TELEMETRY SNAPSHOT:\n{snapshot_summary}\n\n"
                f"{guidance}"
            )})
        else:
            messages.append({"role": "user", "content": (
                f"INCIDENT ALERT: {alert}\n\n"
                "Begin investigation. Start by calling get_error_rates to check for issues."
            )})

        self.evidence_log = []
        self.tool_calls_log = []
        final_report = {}
        last_tool = None
        same_count = 0

        for step in range(1, MAX_ITERATIONS + 1):
            print(f"--- Step {step}/{MAX_ITERATIONS} ---")

            # Get LLM response
            response = ollama_chat(self.model, messages)
            print(f"[Agent] {response[:500]}{'...' if len(response) > 500 else ''}\n")

            messages.append({"role": "assistant", "content": response})

            # Parse tool call
            call = parse_tool_call(response)
            if not call:
                messages.append({"role": "user", "content":
                    "I could not find a tool call in your reply. "
                    "Please emit exactly ONE tool call as a ```json block with 'tool' and 'params' keys."
                })
                continue

            tool_name = call.get("tool", "")
            params = call.get("params", {})

            # Stuck-loop guard
            if tool_name == last_tool:
                same_count += 1
            else:
                same_count = 0
            last_tool = tool_name

            if same_count >= 2:
                messages.append({"role": "user", "content":
                    f"You have called '{tool_name}' {same_count+1} times in a row. "
                    "Move to the NEXT logical investigation step."
                })
                same_count = 0
                continue

            if tool_name not in TOOLS:
                messages.append({"role": "user", "content":
                    f"Unknown tool '{tool_name}'. Available: {list(TOOLS.keys())}"
                })
                continue

            # Handle finish
            if tool_name == "finish":
                final_report = self._build_report(params, incident_id)
                print(f"\n[Detection Agent] Investigation complete.")
                break

            # Execute tool
            print(f"[Tool] {tool_name}({json.dumps(params, default=str)[:200]})")
            try:
                result = TOOLS[tool_name]["fn"](params)
            except Exception as e:
                result = f"Tool error: {e}"

            result_str = json.dumps(result, default=str) if not isinstance(result, str) else result
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "\n...[truncated]"

            print(f"[Result] {result_str[:300]}{'...' if len(result_str) > 300 else ''}\n")

            # Log evidence
            self.tool_calls_log.append({
                "step": step,
                "tool": tool_name,
                "params": params,
                "result_preview": result_str[:500],
            })

            skip_words = ("not found", "failed", "error", "no recent")
            if result_str.strip() not in ("[]", "{}", '""') and \
               not all(w in result_str.lower() for w in skip_words):
                self.evidence_log.append({
                    "source": tool_name,
                    "params": params,
                    "data": result_str[:600],
                })

            # Inject result back to LLM with step pressure
            remaining = MAX_ITERATIONS - step
            if remaining <= 2:
                urgency = (
                    f"\n\n⚠️ YOU HAVE {remaining} STEP(S) LEFT. "
                    "You MUST call 'finish' NOW with your best diagnosis based on evidence collected so far. "
                    "Do NOT call any more investigation tools. Call 'finish' immediately."
                )
            elif remaining <= 4:
                urgency = (
                    f"\n\n⏰ {remaining} steps remaining. "
                    "If you have enough evidence, call 'finish' now. Don't waste steps."
                )
            else:
                urgency = ""

            messages.append({"role": "user", "content":
                f"Result of {tool_name}:\n{result_str}\n\n"
                "Analyze this result. What does it tell you? "
                "Then pick the next tool to call, or call 'finish' if you have enough evidence."
                f"{urgency}"
            })
        else:
            # Max iterations reached — auto-build report from collected evidence
            final_report = self._auto_finish(incident_id)

        return final_report

    def _build_report(self, params: dict, incident_id: str) -> dict:
        """Build the structured detection report."""
        # Parse possible_causes — LLM might return string or list
        possible_causes = params.get("possible_causes", [])
        if isinstance(possible_causes, str):
            possible_causes = [c.strip() for c in possible_causes.split(",") if c.strip()]

        return {
            "incident_id": incident_id,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "detection": {
                "findings_summary": params.get("findings_summary", params.get("evidence_summary", "")),
                "confidence": params.get("confidence", "low"),
                "affected_services": self._parse_services(params.get("affected_services", "")),
                "possible_causes": possible_causes,
                "recommended_action": params.get("recommended_action", params.get("proposed_action", "escalate_to_human(insufficient evidence)")),
            },
            "evidence": self.evidence_log,
            "investigation_log": self.tool_calls_log,
            "model_used": self.model,
        }

    def _auto_finish(self, incident_id: str) -> dict:
        """When LLM didn't call finish, summarize evidence into a report."""
        evidence_summary = "\n".join(
            f"- {e['source']}({json.dumps(e['params'])}): {e['data'][:300]}"
            for e in self.evidence_log[:8]
        )

        # Gather symptoms from evidence for KB pattern matching
        symptom_texts = [e['data'][:200] for e in self.evidence_log[:8]]
        kb_patterns = match_failure_patterns(symptom_texts)
        kb_hint = ""
        if kb_patterns:
            kb_hint = "\n\nKNOWLEDGE BASE HINTS:\n" + "\n".join(
                f"- {p['id']}: {p['interpretation']}" for p in kb_patterns[:2]
            )

        prompt = (
            "You ran out of investigation steps. Summarize ALL the evidence collected below.\n\n"
            f"EVIDENCE:\n{evidence_summary}{kb_hint}\n\n"
            "REMEMBER: frontend-proxy is almost never the root cause. "
            "504 timeouts mean a DOWNSTREAM service is the real problem.\n"
            "If you're not sure of the root cause, list possible causes — don't guess.\n\n"
            "Respond with ONLY a JSON object (no other text):\n"
            '```json\n'
            '{"findings_summary": "what the data shows", "confidence": "high/medium/low", '
            '"affected_services": "svc1,svc2", '
            '"possible_causes": ["cause1", "cause2"], '
            '"recommended_action": "rollback_service(x) or escalate_to_human(reason)"}\n'
            '```'
        )
        response = ollama_chat(self.model, [
            {"role": "system", "content": "You are an SRE agent. Summarize investigation findings honestly."},
            {"role": "user", "content": prompt},
        ], temperature=0.05)

        print(f"[Auto-finish] LLM summary: {response[:300]}")

        call = parse_tool_call(f'{{"tool":"finish","params":{response}}}')
        if not call:
            try:
                params = json.loads(response.strip().strip('`').replace('json\n', '').strip())
            except Exception:
                call = parse_tool_call(response)
                params = call.get("params", {}) if call else {}

            if not params:
                params = {
                    "findings_summary": evidence_summary[:500],
                    "confidence": "low",
                    "affected_services": "",
                    "possible_causes": ["auto-summary failed — see evidence log"],
                    "recommended_action": "escalate_to_human(auto-summary failed)",
                }
        else:
            params = call.get("params", {})

        return self._build_report(params, incident_id)

    @staticmethod
    def _parse_services(val) -> list[str]:
        """Handle affected_services whether LLM returns a string or a list."""
        if isinstance(val, list):
            return [str(s).strip() for s in val if str(s).strip()]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return []

    def _summarize_snapshot(self, snapshot: dict) -> str:
        """Create a concise summary of the initial snapshot for the LLM."""
        parts = []

        # Unhealthy services
        if snapshot.get("unhealthy_services"):
            parts.append("UNHEALTHY CONTAINERS:")
            for u in snapshot["unhealthy_services"]:
                parts.append(f"  - {u['name']}: status={u['status']}, restarts={u.get('restart_count', 0)}")

        # Anomalies
        if snapshot.get("anomalies"):
            parts.append("\nANOMALIES DETECTED:")
            for a in snapshot["anomalies"]:
                parts.append(f"  - {a['type']}: service={a['service']}, "
                           f"protocol={a.get('protocol','?')}, value={a.get('value', 0):.4f}")

        # Error rates
        if snapshot.get("error_rates"):
            active = [e for e in snapshot["error_rates"] if e.get("error_rate", 0) > 0]
            if active:
                parts.append("\nERROR RATES (non-zero):")
                for e in active:
                    parts.append(f"  - {e['service']} ({e.get('protocol','?')}): {e['error_rate']:.4f}")

        # Error logs
        if snapshot.get("error_logs"):
            parts.append("\nERROR LOGS:")
            for svc, logs in snapshot["error_logs"].items():
                parts.append(f"  [{svc}] ({len(logs)} error lines)")
                for log in logs[:5]:
                    parts.append(f"    {str(log)[:150]}")

        if not parts:
            parts.append("No anomalies detected in initial snapshot. System appears healthy.")

        return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Standalone test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Collect a snapshot first so the agent starts with context
    print("Collecting initial telemetry snapshot...")
    snapshot = collector.collect_full_snapshot()

    anomalies = snapshot.get("anomalies", [])
    unhealthy = snapshot.get("unhealthy_services", [])

    if anomalies or unhealthy:
        parts = []
        for a in anomalies:
            parts.append(f"{a['service']} has {a['type']} ({a.get('protocol','?')}, value={a.get('value', 0):.4f})")
        for u in unhealthy:
            parts.append(f"{u['name']} is {u.get('status','down')} (restarts={u.get('restart_count',0)})")
        alert = "Anomalies detected: " + "; ".join(parts)
    else:
        alert = sys.argv[1] if len(sys.argv) > 1 else \
            "Routine health check — no anomalies in initial snapshot."

    print(f"Alert: {alert}\n")

    agent = DetectionAgent()
    report = agent.investigate(alert, incident_id="INC-TEST-001", initial_snapshot=snapshot)

    print(f"\n{'='*70}")
    print("DETECTION REPORT")
    print(f"{'='*70}")
    print(json.dumps(report, indent=2, default=str))

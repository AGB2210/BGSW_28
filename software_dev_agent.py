#!/usr/bin/env python3
"""
software_dev_agent.py - Software Dev Agent for fault remediation.

Takes the detection agent's report, deep-dives into the codebase to pinpoint
the exact source of the fault, proposes a fix, presents it to the user for
approval, and applies the approved fix.

Uses a local Ollama LLM optimized for code understanding.
"""

import json
import re
import sys
import datetime
from typing import Optional
from repo_context import get_repo_overview, get_service_source, is_blocked
from telemetry_collector import TelemetryCollector
from knowledge_base import (
    get_service_info,
    get_dependencies,
    get_dependents,
    find_root_cause_candidates,
    match_failure_patterns,
    format_dependency_map_for_prompt,
)

# ═══════════════════════════════════════════════════════════════════════════════
# LLM Configuration
# ═══════════════════════════════════════════════════════════════════════════════

# Code-focused model for the dev agent
DEV_MODEL = "qwen2.5-coder:7b"
OLLAMA_URL = "http://localhost:11434"
MAX_ITERATIONS = 8

import requests as _requests

def ollama_chat(model: str, messages: list[dict], temperature: float = 0.1) -> str:
    """Send a chat request to Ollama."""
    try:
        r = _requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 3000,
                "num_ctx": 8192,
            }
        }, timeout=180)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")
    except Exception as e:
        return f"[LLM ERROR] {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Definitions for Dev Agent
# ═══════════════════════════════════════════════════════════════════════════════

collector = TelemetryCollector()

def _make_tools(repo_root: str) -> dict:
    """Create tool definitions with repo_root bound."""
    return {
        "get_repo_overview": {
            "desc": "Get high-level overview of all services in the repo (languages, paths, descriptions).",
            "params": [],
            "fn": lambda p: get_repo_overview(repo_root),
        },
        "read_service_source": {
            "desc": "Read the main source files of a service. Params: service_name (e.g. 'cart', 'payment').",
            "params": ["service_name"],
            "fn": lambda p: get_service_source(repo_root, p["service_name"], max_lines=400),
        },
        "get_error_logs": {
            "desc": "Get ERROR/WARN log lines from a running container. Params: service_name",
            "params": ["service_name"],
            "fn": lambda p: collector.get_service_logs(p["service_name"], level="warn"),
        },
        "get_traces": {
            "desc": "Get recent distributed traces for a service. Params: service_name",
            "params": ["service_name"],
            "fn": lambda p: collector.get_traces(p["service_name"], limit=5),
        },
        "get_container_stats": {
            "desc": "Get CPU/memory usage of a container. Params: service_name",
            "params": ["service_name"],
            "fn": lambda p: collector.get_container_stats(p["service_name"]),
        },
        "query_metric": {
            "desc": "Run a PromQL query against Prometheus. Params: promql",
            "params": ["promql"],
            "fn": lambda p: collector.prom_query(p["promql"]),
        },
        "get_service_dependencies": {
            "desc": "Query the knowledge base for a service's dependencies, dependents, language, and role. Params: service_name",
            "params": ["service_name"],
            "fn": lambda p: get_service_info(p["service_name"]) or {"error": f"Unknown service: {p['service_name']}"},
        },
        "finish": {
            "desc": "Submit diagnosis and fix proposal. Params: pinpoint_location, root_cause_detail, recommended_fix, fix_type, fix_command",
            "params": ["pinpoint_location", "root_cause_detail", "recommended_fix", "fix_type", "fix_command"],
            "fn": None,
        },
    }


TOOLS_DOC_TEMPLATE = lambda tools: "\n".join(
    f"  - {name}({', '.join(d['params'])}): {d['desc']}"
    for name, d in tools.items()
)

# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

def build_system_prompt(tools: dict) -> str:
    return f"""You are a specialist software engineer agent for the OpenTelemetry Astronomy Shop demo.
You receive a detection report from the detection agent and must:
1. Deep-dive into the codebase to pinpoint the EXACT source of the fault
2. Propose a specific, safe remediation fix

## CRITICAL RULES
- ALL diagnosis comes from telemetry data and source code analysis
- You have NO access to internal service configurations — diagnose from observable signals only
- Your fix must use the Action API: restart_service, rollback_service, throttle_load_generator, scale_service
- Emit exactly ONE tool call per reply as a ```json block
- Be specific about file paths, function names, and error patterns

## FIX TYPES (for the finish tool)
- restart_service: Service needs restart (crash loop, stuck, OOM)
- rollback_service: Service needs to be rolled back to a previous version
- scale_service: Service needs more replicas
- throttle_load: Load generator needs throttling
- resource_fix: Container resource limits need adjustment
- escalate: Cannot determine fix, needs human

## TOOL CALL FORMAT
```json
{{"tool": "<tool_name>", "params": {{"key": "value"}}}}
```

## AVAILABLE TOOLS
{TOOLS_DOC_TEMPLATE(tools)}

## ARCHITECTURE KNOWLEDGE
{format_dependency_map_for_prompt()}

Be precise. Reference specific files, functions, error messages.
The user will review your diagnosis before any fix is applied.
"""


def parse_tool_call(text: str) -> Optional[dict]:
    """Extract tool call from LLM response."""
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
# Software Dev Agent
# ═══════════════════════════════════════════════════════════════════════════════

class SoftwareDevAgent:
    """
    Specialist agent that analyzes code to pinpoint faults and propose fixes.
    """

    def __init__(self, repo_root: str, model: str = DEV_MODEL):
        self.repo_root = repo_root
        self.model = model
        self.tools = _make_tools(repo_root)
        self.investigation_log = []

    def diagnose_and_propose_fix(self, detection_report: dict) -> dict:
        """
        Given a detection report, pinpoint the fault in code and propose a fix.

        Returns a structured diagnosis + fix proposal.
        """
        incident_id = detection_report.get("incident_id", "INC-???")
        detection = detection_report.get("detection", {})

        print(f"\n{'='*70}")
        print(f"  SOFTWARE DEV AGENT - Diagnosing: {incident_id}")
        print(f"  Detection: {detection.get('root_cause', 'unknown')}")
        print(f"  Affected: {detection.get('affected_services', [])}")
        print(f"{'='*70}\n")

        system_prompt = build_system_prompt(self.tools)

        # Build context from detection report
        detection_summary = self._format_detection_report(detection_report)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"DETECTION AGENT REPORT:\n{detection_summary}\n\n"
                "Your job: pinpoint the EXACT source of this fault in the codebase.\n"
                "Start by calling get_repo_overview to understand the project structure, "
                "then read the source code of the affected services."
            )}
        ]

        self.investigation_log = []
        final_diagnosis = {}

        for step in range(1, MAX_ITERATIONS + 1):
            print(f"--- Dev Step {step}/{MAX_ITERATIONS} ---")

            response = ollama_chat(self.model, messages)
            print(f"[DevAgent] {response[:400]}{'...' if len(response) > 400 else ''}\n")

            messages.append({"role": "assistant", "content": response})

            call = parse_tool_call(response)
            if not call:
                messages.append({"role": "user", "content":
                    "Please emit a tool call as a ```json block with 'tool' and 'params'."
                })
                continue

            tool_name = call.get("tool", "")
            params = call.get("params", {})

            if tool_name not in self.tools:
                messages.append({"role": "user", "content":
                    f"Unknown tool '{tool_name}'. Available: {list(self.tools.keys())}"
                })
                continue

            if tool_name == "finish":
                final_diagnosis = self._build_diagnosis(params, detection_report)
                break

            # Execute tool
            print(f"[Tool] {tool_name}({json.dumps(params, default=str)[:200]})")
            try:
                result = self.tools[tool_name]["fn"](params)
            except Exception as e:
                result = f"Tool error: {e}"

            result_str = json.dumps(result, default=str) if not isinstance(result, str) else result
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "\n...[truncated]"

            print(f"[Result] {result_str[:300]}...\n")

            self.investigation_log.append({
                "step": step,
                "tool": tool_name,
                "params": params,
            })

            messages.append({"role": "user", "content":
                f"Result of {tool_name}:\n{result_str}\n\n"
                "Analyze this. What does it reveal about the fault? "
                "Continue investigating or call 'finish' with your diagnosis."
            })
        else:
            final_diagnosis = self._build_diagnosis({
                "pinpoint_location": "Could not determine",
                "root_cause_detail": "Investigation budget exhausted",
                "recommended_fix": "Manual investigation needed",
                "fix_type": "escalate",
                "fix_command": "escalate_to_human(dev agent inconclusive)",
            }, detection_report)

        return final_diagnosis

    def _format_detection_report(self, report: dict) -> str:
        """Format detection report for the dev agent's context."""
        detection = report.get("detection", {})
        parts = [
            f"Incident ID: {report.get('incident_id', '?')}",
            f"Root Cause (detected): {detection.get('root_cause', '?')}",
            f"Confidence: {detection.get('confidence', '?')}",
            f"Affected Services: {', '.join(detection.get('affected_services', []))}",
            f"Evidence Summary: {detection.get('evidence_summary', '?')}",
            f"Proposed Action: {detection.get('proposed_action', '?')}",
        ]

        # Include key evidence
        evidence = report.get("evidence", [])
        if evidence:
            parts.append("\nKEY EVIDENCE:")
            for e in evidence[:5]:
                parts.append(f"  [{e.get('source', '?')}] {str(e.get('data', ''))[:300]}")

        return "\n".join(parts)

    def _build_diagnosis(self, params: dict, detection_report: dict) -> dict:
        """Build the structured diagnosis + fix proposal."""
        return {
            "incident_id": detection_report.get("incident_id", "?"),
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "detection_summary": {
                "root_cause": detection_report.get("detection", {}).get("root_cause", "?"),
                "confidence": detection_report.get("detection", {}).get("confidence", "?"),
                "evidence_summary": detection_report.get("detection", {}).get("evidence_summary", "?"),
                "affected_services": detection_report.get("detection", {}).get("affected_services", []),
            },
            "dev_diagnosis": {
                "pinpoint_location": params.get("pinpoint_location", "unknown"),
                "root_cause_detail": params.get("root_cause_detail", "unknown"),
                "recommended_fix": params.get("recommended_fix", "unknown"),
                "fix_type": params.get("fix_type", "escalate"),
                "fix_command": params.get("fix_command", "escalate_to_human(unknown)"),
            },
            "investigation_log": self.investigation_log,
            "model_used": self.model,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# User Interaction for Approval
# ═══════════════════════════════════════════════════════════════════════════════

def present_diagnosis_to_user(diagnosis: dict) -> dict:
    """
    Present the full diagnosis to the user and get their decision.
    Returns: {"approved": bool, "user_input": str, "action": str}
    """
    det = diagnosis.get("detection_summary", {})
    dev = diagnosis.get("dev_diagnosis", {})

    print(f"\n{'='*70}")
    print(f"  INCIDENT DIAGNOSIS - {diagnosis.get('incident_id', '?')}")
    print(f"{'='*70}")
    print()
    print("--- DETECTION AGENT FINDINGS ---")
    print(f"  Root Cause:    {det.get('root_cause', '?')}")
    print(f"  Confidence:    {det.get('confidence', '?')}")
    print(f"  Affected:      {', '.join(det.get('affected_services', []))}")
    print(f"  Evidence:      {det.get('evidence_summary', '?')}")
    print()
    print("--- SOFTWARE DEV ASSESSMENT ---")
    print(f"  Pinpoint:      {dev.get('pinpoint_location', '?')}")
    print(f"  Detail:        {dev.get('root_cause_detail', '?')}")
    print(f"  Recommended:   {dev.get('recommended_fix', '?')}")
    print(f"  Fix Type:      {dev.get('fix_type', '?')}")
    print(f"  Fix Command:   {dev.get('fix_command', '?')}")
    print()
    print(f"{'='*70}")
    print("OPTIONS:")
    print("  1. APPROVE - Execute the recommended fix")
    print("  2. MODIFY  - Approve with changes (type your modification)")
    print("  3. REJECT  - Do not apply any fix")
    print("  4. KILL    - Emergency stop")
    print(f"{'='*70}")

    while True:
        choice = input("\nYour choice [1/2/3/4]: ").strip()

        if choice == "1":
            return {
                "approved": True,
                "user_input": "",
                "action": dev.get("fix_command", ""),
                "modified": False,
            }
        elif choice == "2":
            user_mod = input("Enter your modification/alternate fix command: ").strip()
            return {
                "approved": True,
                "user_input": user_mod,
                "action": user_mod,
                "modified": True,
            }
        elif choice == "3":
            reason = input("Reason for rejection (optional): ").strip()
            return {
                "approved": False,
                "user_input": reason,
                "action": "",
                "modified": False,
            }
        elif choice == "4":
            print("\n  KILL SWITCH ACTIVATED. Stopping immediately.")
            sys.exit(0)
        else:
            print("  Invalid choice. Enter 1, 2, 3, or 4.")


# ═══════════════════════════════════════════════════════════════════════════════
# Standalone test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Example detection report for testing
    test_report = {
        "incident_id": "INC-TEST-001",
        "detection": {
            "root_cause": "Cart service returning 100% errors on all gRPC calls",
            "confidence": "high",
            "evidence_summary": "gRPC error rate at 1.0, all cart operations failing with INTERNAL error",
            "affected_services": ["cart", "checkout", "frontend"],
            "proposed_action": "restart_service(cart)",
        },
        "evidence": [
            {"source": "get_error_rates", "data": "cart gRPC error rate: 1.0"},
            {"source": "get_error_logs", "data": "ERROR: GetCart failed: simulated failure"},
        ],
    }

    repo_root = sys.argv[1] if len(sys.argv) > 1 else "."
    agent = SoftwareDevAgent(repo_root)
    diagnosis = agent.diagnose_and_propose_fix(test_report)

    print(json.dumps(diagnosis, indent=2, default=str))

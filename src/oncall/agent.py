"""Investigation loop: the model picks one tool at a time until it can diagnose."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from oncall import llm, tools
from oncall.config import get_settings
from oncall.evidence import Ledger
from oncall.schema import Diagnosis

SYSTEM_PROMPT = """You are an on-call engineer investigating an incident in the OpenTelemetry Demo, \
a microservices web shop. Use the tools to gather evidence, one call at a time, choosing the next \
tool based on what you have found so far. Separate root causes from downstream symptoms.

How to investigate:
- Start with service_overview to see which services have errors or high latency.
- Use search_traces on the service named in the alert first, then on other affected services. A failure usually STARTS in the deepest failing call \
(failure_origins); services above it only pass the error up.
- Check the suspected service with search_logs and container_health. Use deployment_history to see \
whether something restarted, stopped or changed just before the problem.
- A stopped, crashing or resource-starved container, or an unreachable database or cache, is an \
infrastructure cause. An error message inside a running healthy service is an application cause.

Rules:
- Every claim must cite evidence IDs (like ev-002) returned by your tool calls. Never invent data.
- If the evidence is not conclusive, set escalate=true and say what a human should check next.

When you have enough evidence, stop calling tools and say so."""

FINAL_PROMPT = """Give your final diagnosis as JSON. Cite only evidence IDs returned by your tool calls. \
If the evidence does not clearly show one root cause, set escalate=true, use a low confidence and \
list what a human should check next."""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _escalation(reason: str) -> Diagnosis:
    return Diagnosis(component=None, cause=reason, confidence=0.0, escalate=True,
                     next_steps=["Review the evidence ledger manually"])


def investigate(alert: str, ledger: Ledger | None = None, verbose: bool = False) -> tuple[Diagnosis, Ledger]:
    try:
        return _investigate(alert, ledger or Ledger(), verbose)
    finally:
        llm.unload()


def _investigate(alert: str, ledger: Ledger, verbose: bool) -> tuple[Diagnosis, Ledger]:
    settings = get_settings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Alert: {alert}"},
    ]

    nudged = False
    for _ in range(settings.max_steps):
        reply = llm.chat(messages, tools.specs())
        messages.append(reply)
        calls = reply.get("tool_calls") or []
        if not calls:
            missing = _checklist_gap(ledger)
            if missing and not nudged:  # one reminder, then accept the model's decision
                nudged = True
                messages.append({"role": "user", "content": missing})
                continue
            break
        for call in calls:
            fn = call["function"]
            output = tools.call(fn["name"], fn.get("arguments") or {}, ledger)
            if verbose:
                print(f"[tool] {fn['name']}({fn.get('arguments')}) -> {output.get('evidence_id', output.get('error'))}")
            messages.append({"role": "tool", "content": json.dumps(output, default=str)})
    else:
        messages.append({"role": "user", "content": f"Step limit ({settings.max_steps}) reached."})

    # Final answer with the output format enforced, so it always parses.
    messages.append({"role": "user", "content": FINAL_PROMPT})
    final = llm.chat(messages, schema=Diagnosis.model_json_schema())
    return _finalise(final.get("content", ""), ledger, settings.min_confidence), ledger


def _checklist_gap(ledger: Ledger) -> str | None:
    """A broad infrastructure check is required before concluding."""
    if not any(e.tool == "container_health" and not e.args.get("name") for e in ledger.items()):
        return ("Before concluding, call container_health with no name to check every container for "
                "stopped, restarting, CPU-starved or memory-starved services.")
    return None


def _corroboration_problem(diagnosis: Diagnosis, ledger: Ledger) -> str | None:
    """The blamed component must appear in evidence from 2+ different tools.

    Supporting evidence the model gathered but forgot to cite is added to its citations.
    """
    if not diagnosis.component:
        return "no component named"
    supporting = [e for e in ledger.items() if diagnosis.component in json.dumps(e.summary, default=str)]
    tools_seen = {e.tool for e in supporting}
    if len(tools_seen) < 2:
        return f"{diagnosis.component} appears in evidence from {len(tools_seen)} tool(s); need 2"
    for e in supporting:
        if e.id not in diagnosis.evidence:
            diagnosis.evidence.append(e.id)
    return None


def _finalise(content: str, ledger: Ledger, min_confidence: float) -> Diagnosis:
    data = _extract_json(content)
    if data is None:
        return _escalation("Model did not return a JSON diagnosis")
    try:
        diagnosis = Diagnosis.model_validate(data)
    except ValidationError as exc:
        return _escalation(f"Diagnosis failed schema validation: {exc.error_count()} errors")

    invented = ledger.missing(diagnosis.evidence)
    if invented or not diagnosis.evidence:
        diagnosis.escalate = True
        diagnosis.next_steps.insert(0, f"Rejected citations: {invented or 'none given'}")
    weak = _corroboration_problem(diagnosis, ledger)
    if weak:
        diagnosis.confidence = min(diagnosis.confidence, 0.5)
        diagnosis.next_steps.insert(0, f"Not corroborated: {weak}")
    if diagnosis.confidence < min_confidence:
        diagnosis.escalate = True
    return diagnosis

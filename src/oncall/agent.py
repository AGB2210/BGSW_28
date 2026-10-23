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

Rules:
- Every claim must cite evidence IDs (like ev-002) returned by your tool calls. Never invent data.
- If the evidence is not conclusive, set escalate=true and say what a human should check next.

When done, reply with ONLY a JSON object with these keys:
component (string or null), cause (string), confidence (0-1), evidence (list of IDs),
escalate (bool), next_steps (list of strings)."""


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
    settings = get_settings()
    ledger = ledger or Ledger()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Alert: {alert}"},
    ]

    for _ in range(settings.max_steps):
        reply = llm.chat(messages, tools.specs())
        messages.append(reply)
        calls = reply.get("tool_calls") or []
        if not calls:
            return _finalise(reply.get("content", ""), ledger, settings.min_confidence), ledger
        for call in calls:
            fn = call["function"]
            output = tools.call(fn["name"], fn.get("arguments") or {}, ledger)
            if verbose:
                print(f"[tool] {fn['name']}({fn.get('arguments')}) -> {output.get('evidence_id', output.get('error'))}")
            messages.append({"role": "tool", "content": json.dumps(output, default=str)})

    return _escalation(f"No conclusion within {settings.max_steps} steps"), ledger


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
    if diagnosis.confidence < min_confidence:
        diagnosis.escalate = True
    return diagnosis

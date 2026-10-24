"""Investigation loop: the model picks one tool at a time until it can diagnose."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import ValidationError

from oncall import llm, remedy, tools
from oncall.tools import containers
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
- If a container is stopped or crashing, the services that call it fail too; blame the stopped one.
- load-generator is the simulated users. Its errors and high CPU are symptoms, not causes.
- Slow calls to a service that is fast inside (its own spans are quick) point to the network between them.

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


class Cancelled(Exception):
    """The operator stopped the investigation."""


def investigate(alert: str, ledger: Ledger | None = None, verbose: bool = False,
                on_event: Callable[[str, dict], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> tuple[Diagnosis, Ledger]:
    """Investigate an alert. `on_event(kind, data)` receives each tool call as it happens.

    `should_stop()` is checked before every model and tool call; when it returns True
    the investigation raises Cancelled.
    """
    try:
        return _investigate(alert, ledger or Ledger(), verbose, on_event or (lambda kind, data: None),
                            should_stop or (lambda: False))
    finally:
        llm.unload()


def _investigate(alert: str, ledger: Ledger, verbose: bool, on_event: Callable[[str, dict], None],
                 should_stop: Callable[[], bool]) -> tuple[Diagnosis, Ledger]:
    def checkpoint() -> None:
        if should_stop():
            raise Cancelled("investigation cancelled by the operator")

    settings = get_settings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Alert: {alert}"},
    ]

    nudged = False
    for _ in range(settings.max_steps):
        checkpoint()
        reply = llm.chat(messages, tools.specs())
        used = llm.LAST_STATS.get("prompt_tokens", 0)
        if verbose:
            print(f"[llm] prompt {used} / {settings.ollama_num_ctx} tokens")
        messages.append(reply)
        calls = reply.get("tool_calls") or []
        if used > CONTEXT_BUDGET * settings.ollama_num_ctx:  # stop before older messages get truncated
            messages.append({"role": "user", "content": "Context is nearly full; stop investigating now."})
            break
        if not calls:
            missing = _checklist_gap(ledger)
            if missing and not nudged:  # one reminder, then accept the model's decision
                nudged = True
                messages.append({"role": "user", "content": missing})
                continue
            break
        for call in calls:
            checkpoint()
            fn = call["function"]
            output = tools.call(fn["name"], fn.get("arguments") or {}, ledger)
            on_event("tool", {"tool": fn["name"], "args": fn.get("arguments") or {},
                              "evidence_id": output.get("evidence_id"), "error": output.get("error")})
            if verbose:
                print(f"[tool] {fn['name']}({fn.get('arguments')}) -> {output.get('evidence_id', output.get('error'))}")
            messages.append({"role": "tool", "content": json.dumps(output, default=str)})
    else:
        messages.append({"role": "user", "content": f"Step limit ({settings.max_steps}) reached."})

    # Infrastructure sweep: if the model never checked every container, do it for it, so a stopped,
    # crashing or starved container can never be missed.
    if _checklist_gap(ledger):
        checkpoint()
        output = tools.call("container_health", {}, ledger)
        on_event("tool", {"tool": "container_health", "args": {}, "evidence_id": output.get("evidence_id"),
                          "error": output.get("error"), "automatic": True})
        if verbose:
            print(f"[tool] container_health({{}}) -> {output.get('evidence_id')} (automatic sweep)")
        messages.append({"role": "user", "content": "Automatic infrastructure check of every container: "
                                                    + json.dumps(output, default=str)})

    # Final answer with the output format enforced, so it always parses.
    checkpoint()
    messages.append({"role": "user", "content": FINAL_PROMPT})
    final = llm.chat(messages, schema=_final_schema(_known_components(ledger)))
    return _finalise(final.get("content", ""), ledger, settings.min_confidence, alert), ledger


def _checklist_gap(ledger: Ledger) -> str | None:
    """A broad infrastructure check is required before concluding."""
    if not any(e.tool == "container_health" and not e.args.get("name") for e in ledger.items()):
        return ("Before concluding, call container_health with no name to check every container for "
                "stopped, restarting, CPU-starved or memory-starved services.")
    return None


TRAFFIC_SOURCE = "load-generator"  # the simulated users: its errors are symptoms of other failures
CONTEXT_BUDGET = 0.75  # share of the context window after which the agent must conclude
# Only a container that is actually down may override the model's diagnosis. A recent restart
# can be a side effect of something earlier, so it supports a diagnosis but never replaces one.
STOPPED = {"not running", "oom killed"}


def _known_components(ledger: Ledger) -> list[str]:
    """Real container and service names, used to constrain the final answer."""
    names: set[str] = set()
    try:
        names.update(containers.docker("ps", "-a", "--format", "{{.Names}}").split())
    except Exception:
        pass
    for e in ledger.items():
        if e.tool == "service_overview" and isinstance(e.summary, list):
            names.update(r.get("service", "") for r in e.summary if isinstance(r, dict))
    return sorted(n for n in names if n)


def _final_schema(names: list[str]) -> dict[str, Any]:
    schema = Diagnosis.model_json_schema()
    if names:
        schema["properties"]["component"] = {"anyOf": [{"type": "string", "enum": names}, {"type": "null"}],
                                             "description": "Failing service or container, or null if unknown"}
    return schema


def _container_rows(ledger: Ledger) -> dict[str, dict]:
    """Latest container_health row per container, from all-container and by-name checks."""
    rows: dict[str, dict] = {}
    for e in ledger.items():
        if e.tool == "container_health" and isinstance(e.summary, dict):
            for r in (e.summary.get("with_problems") or []) + (e.summary.get("containers") or []):
                rows[r["name"]] = r
            for name in e.summary.get("healthy", []):
                rows[name] = {"name": name, "problems": []}
    return rows


def _stopped_containers(ledger: Ledger) -> dict[str, list[str]]:
    """Containers seen down in any container check; 'not running' ones win."""
    found = {n: [p for p in r.get("problems", []) if p in STOPPED]
             for n, r in _container_rows(ledger).items() if STOPPED & set(r.get("problems", []))}
    down = {n: p for n, p in found.items() if "not running" in p}
    return down or found


def _only_troubled_container(ledger: Ledger) -> tuple[str, list[str]] | None:
    """The single container (other than the load generator) that any check showed with problems."""
    troubled = {n: r["problems"] for n, r in _container_rows(ledger).items()
                if r.get("problems") and n != TRAFFIC_SOURCE}
    return next(iter(troubled.items())) if len(troubled) == 1 else None


def _unanswered_callee(ledger: Ledger, known: set[str]) -> tuple[str, str] | None:
    """(caller, callee) when the most common trace failure is a call the callee never answered."""
    for e in reversed(ledger.items()):
        if e.tool != "search_traces" or not isinstance(e.summary, dict):
            continue
        origins = e.summary.get("failure_origins") or []
        if origins and origins[0].get("note") and origins[0].get("calls") in known:
            return origins[0]["service"], origins[0]["calls"]
    return None


EDGE = {"frontend-proxy", TRAFFIC_SOURCE}  # entry points: failures there are seen, not started


def _failure_start(ledger: Ledger, known: set[str]) -> str | None:
    """Service where most traced failures start, with a real error message, ignoring entry points."""
    counts: dict[str, int] = {}
    for e in ledger.items():
        if e.tool == "search_traces" and isinstance(e.summary, dict):
            for o in e.summary.get("failure_origins") or []:
                svc = o.get("service")
                if svc in known and svc not in EDGE and o.get("error", "(no message)") != "(no message)":
                    counts[svc] = counts.get(svc, 0) + int(o.get("count", 1))
    return max(counts, key=counts.get) if counts else None


def _is_upstream_of(ledger: Ledger, caller: str, callee: str) -> bool:
    """True if traced calls show a path caller → … → callee."""
    graph: dict[str, set[str]] = {}
    for e in ledger.items():
        if e.tool == "search_traces" and isinstance(e.summary, dict):
            for edge in e.summary.get("calls") or []:
                a, _, b = edge.partition("→")
                graph.setdefault(a, set()).add(b)
    seen, frontier = {caller}, [caller]
    while frontier:
        for nxt in graph.get(frontier.pop(), ()):
            if nxt == callee:
                return True
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


def _row_abnormal(r: dict) -> bool:
    """Errors, or latency / traffic well above this service's own baseline."""
    p95, base_p95 = r.get("p95_ms") or 0, r.get("base_p95_ms")
    rps, base_rps = r.get("rps") or 0, r.get("base_rps")
    slow = p95 >= 500 and p95 >= 3 * base_p95 if base_p95 else p95 >= 1000
    surge = bool(base_rps) and rps >= 2 * base_rps and rps - base_rps >= 5
    return r.get("error_pct", 0) >= 1 or slow or surge


def _shows_problem(tool: str, summary: Any, component: str) -> bool:
    """Does this tool result show `component` misbehaving (not merely mention it)?"""
    if isinstance(summary, dict) and "error" in summary and len(summary) == 1:
        return False
    if component == TRAFFIC_SOURCE:
        return False  # never root-cause evidence on its own: it reports other services' failures
    if tool == "service_overview":
        return any(r.get("service") == component and _row_abnormal(r) for r in summary if isinstance(r, dict))
    if tool == "search_traces":
        return any(o.get("service") == component or o.get("calls") == component
                   for o in summary.get("failure_origins", []))
    if tool == "search_logs":
        return any(m.get("service") == component for m in summary.get("distinct_messages", []))
    if tool == "container_health":
        rows = summary.get("with_problems") or summary.get("containers") or []
        return any(r.get("name") == component and r.get("problems") for r in rows)
    if tool == "deployment_history":
        bad = {"die", "kill", "oom", "stop", "restart"}
        return any(e.get("container") == component and e.get("action") in bad
                   for e in summary.get("recent_events", []))
    return False


def _shows_any_failure(e) -> bool:
    """Errors anywhere other than the load generator."""
    s = e.summary
    if e.tool == "service_overview" and isinstance(s, list):
        return any(r.get("error_pct", 0) >= 1 for r in s if isinstance(r, dict) and r.get("service") != TRAFFIC_SOURCE)
    if e.tool == "search_traces" and isinstance(s, dict):
        return bool(s.get("failure_origins"))
    if e.tool == "search_logs" and isinstance(s, dict):
        return any(m.get("service") != TRAFFIC_SOURCE for m in s.get("distinct_messages", []))
    return False


def _named_in_alert(component: str, alert: str) -> bool:
    return bool(alert) and re.search(rf"(^|[;\s]){re.escape(component)}:", alert) is not None


def _corroboration_problem(diagnosis: Diagnosis, ledger: Ledger, alert: str = "") -> str | None:
    """Evidence from 2+ different sources must show the blamed component misbehaving.

    Sources are the tools plus the detector's alert. Supporting evidence the model
    gathered but forgot to cite is added to its citations.
    """
    if not diagnosis.component:
        return "no component named"
    supporting = [e for e in ledger.items() if _shows_problem(e.tool, e.summary, diagnosis.component)]
    alert_witness = _named_in_alert(diagnosis.component, alert)
    hop = _unanswered_callee(ledger, {diagnosis.component})
    if diagnosis.component in _stopped_containers(ledger):
        # Databases and caches have no telemetry of their own; the failures they cause are the second witness.
        supporting += [e for e in ledger.items() if e.tool != "container_health" and _shows_any_failure(e)]
    elif hop and hop[1] == diagnosis.component:
        # A service that never answers records nothing itself; its callers' failures are the second witness.
        supporting += [e for e in ledger.items() if e.tool != "search_traces" and _shows_any_failure(e)]
    tools_seen = {e.tool for e in supporting} | ({"detector alert"} if alert_witness else set())
    if len(tools_seen) < 2:
        return f"only {len(tools_seen)} source(s) show a problem in {diagnosis.component}; need 2"
    for e in supporting:
        if e.id not in diagnosis.evidence:
            diagnosis.evidence.append(e.id)
    return None


def _finalise(content: str, ledger: Ledger, min_confidence: float, alert: str = "") -> Diagnosis:
    data = _extract_json(content)
    if data is None:
        return _escalation("Model did not return a JSON diagnosis")
    try:
        diagnosis = Diagnosis.model_validate(data)
    except ValidationError as exc:
        return _escalation(f"Diagnosis failed schema validation: {exc.error_count()} errors")

    # Infrastructure first: a container that is down explains failures in the services that use it.
    stopped = _stopped_containers(ledger)
    if len(stopped) == 1 and diagnosis.component not in stopped:
        culprit, problems = next(iter(stopped.items()))
        blamed = diagnosis.component or "the alerting services"
        diagnosis.next_steps.insert(0, f"Model first blamed {blamed}; {culprit} is {', '.join(problems)}, "
                                       f"so {blamed} is treated as a symptom")
        diagnosis.component, diagnosis.category = culprit, "infrastructure"
        diagnosis.next_steps.append(f"Model's original explanation: {diagnosis.cause}")
        diagnosis.cause = f"{culprit} is {', '.join(problems)}; services that depend on it fail."
    else:
        # The deepest failure is a call nobody answered: the callee is the suspect, not the caller.
        hop = _unanswered_callee(ledger, set(_known_components(ledger)))
        if hop and diagnosis.component == hop[0]:
            caller, callee = hop
            diagnosis.next_steps.insert(0, f"Model first blamed {caller}; its calls to {callee} got no answer, "
                                           f"so {caller} is treated as a symptom")
            diagnosis.component = callee
            diagnosis.next_steps.append(f"Model's original explanation: {diagnosis.cause}")
            diagnosis.cause = f"{callee} does not answer calls from {caller}."
        elif (start := _failure_start(ledger, set(_known_components(ledger)))) and start != diagnosis.component \
                and diagnosis.component not in (None, start) and _is_upstream_of(ledger, diagnosis.component, start):
            # Failures start in another service that the blamed one depends on: blame where they start.
            diagnosis.next_steps.insert(0, f"Model first blamed {diagnosis.component}; traced failures start in "
                                           f"{start}, so {diagnosis.component} is treated as a symptom")
            diagnosis.next_steps.append(f"Model's original explanation: {diagnosis.cause}")
            diagnosis.cause = f"Traced failures start in {start}; {diagnosis.component} only passes them on."
            diagnosis.component = start
        else:
            # Nothing supports the model's pick, but exactly one container shows a problem: use it.
            lone = _only_troubled_container(ledger)
            unsupported = not diagnosis.component or not any(
                _shows_problem(e.tool, e.summary, diagnosis.component) for e in ledger.items())
            if lone and unsupported and lone[0] != diagnosis.component:
                culprit, problems = lone
                diagnosis.next_steps.insert(0, f"Model first blamed {diagnosis.component or 'nothing'}, which no "
                                               f"evidence supports; {culprit} is the only container with problems "
                                               f"({', '.join(problems)})")
                diagnosis.component = culprit
                diagnosis.next_steps.append(f"Model's original explanation: {diagnosis.cause}")
                diagnosis.cause = f"{culprit}: {', '.join(problems)}."

    # A pure traffic flood is stated as such: the model tends to explain a surge as some other fault.
    surge = remedy.pure_flood(diagnosis.component, alert, ledger)
    if surge and not _container_rows(ledger).get(diagnosis.component or "", {}).get("problems"):
        diagnosis.next_steps.append(f"Model's original explanation: {diagnosis.cause}")
        diagnosis.cause = f"Traffic flood: {diagnosis.component} receives {surge}, while nothing is slow or failing."

    # The category follows the container evidence when the blamed container itself is broken
    # (high CPU or a recent restart can come from the application, so those do not count).
    problems = _container_rows(ledger).get(diagnosis.component or "", {}).get("problems", [])
    if any(p in STOPPED or p == "unhealthy" or p.startswith(("cpu limited", "network delay")) for p in problems):
        diagnosis.category = "infrastructure"

    invented = ledger.missing(diagnosis.evidence)
    if invented or not diagnosis.evidence:
        diagnosis.escalate = True
        diagnosis.next_steps.insert(0, f"Rejected citations: {invented or 'none given'}")
    weak = _corroboration_problem(diagnosis, ledger, alert)
    if weak:
        diagnosis.confidence = min(diagnosis.confidence, 0.5)
        diagnosis.next_steps.insert(0, f"Not corroborated: {weak}")
    if diagnosis.confidence < min_confidence:
        diagnosis.escalate = True
    return diagnosis

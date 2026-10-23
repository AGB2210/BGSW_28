"""One incident end to end: investigate, propose, approve, act, verify, roll back, report.

Every step emits an event so a CLI or the web console can show it live.
The approver callback is the human gate: nothing changes without its yes.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from oncall import actions, detector, remedy, safety
from oncall.agent import Cancelled, investigate
from oncall.config import get_settings
from oncall.evidence import Ledger
from oncall.schema import (Approval, Diagnosis, EvidenceItem, Execution, IncidentReport, ProposedAction,
                           TimelineEntry, Verification)

REPORT_DIR = Path("reports")
EventSink = Callable[[str, dict], None]
Approver = Callable[[ProposedAction, IncidentReport], tuple[bool, str]]  # -> (approved, approver name)

PREVENTION = {
    "restart_service": [
        "Alert on container restarts and failing health checks, not only on user-facing errors",
        "Review the service's memory and CPU limits against its real usage",
    ],
    "throttle_load_generator": [
        "Add rate limiting at the frontend proxy",
        "Alert on request-rate spikes per client",
    ],
    None: [
        "Add a runbook entry for this failure mode once a human confirms the cause",
        "Alert on the first symptom seen in this investigation",
    ],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(value: object, limit: int = 600) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def verify_recovery(component: str, timeout_s: int = 180, every_s: int = 20,
                    should_stop: Callable[[], bool] = lambda: False) -> Verification:
    """Recovered when the detector no longer flags the component against the baseline."""
    checks = []
    deadline = time.time() + timeout_s
    while True:
        if should_stop():
            raise Cancelled("recovery check cancelled by the operator")
        found = [a for a in detector.anomalies(detector.load_baseline(), detector.snapshot("1m"))
                 if a["service"] == component]
        checks.append(f"{_now():%H:%M:%S} " + ("; ".join(a["detail"] for a in found) if found
                                               else f"{component} back to baseline"))
        if not found:
            return Verification(recovered=True, checks=checks)
        if time.time() >= deadline:
            return Verification(recovered=False, checks=checks)
        for _ in range(every_s):  # sleep in 1 s steps so a cancel takes effect quickly
            if should_stop():
                break
            time.sleep(1)


def respond(alert: str, approver: Approver, on_event: EventSink | None = None,
            dry_run: bool | None = None, should_stop: Callable[[], bool] | None = None) -> IncidentReport:
    emit = on_event or (lambda kind, data: None)
    stop = should_stop or (lambda: False)
    dry_run = get_settings().dry_run if dry_run is None else dry_run
    incident_id = f"inc-{_now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    timeline: list[TimelineEntry] = []

    def log(event: str, kind: str, data: dict) -> None:
        timeline.append(TimelineEntry(at=_now(), event=event))
        emit(kind, {"incident_id": incident_id, "event": event, **data})

    log(f"Alert: {alert}", "alert", {"alert": alert})
    try:
        return _respond(alert, approver, emit, dry_run, stop, incident_id, timeline, log)
    except Cancelled as exc:
        log(f"Cancelled by the operator: {exc}", "cancelled", {"reason": str(exc)})
        report = IncidentReport(
            incident_id=incident_id, alert=alert, detected_at=timeline[0].at, status="cancelled",
            diagnosis=Diagnosis(component=None, cause="Investigation cancelled before a conclusion",
                                confidence=0.0, escalate=True, next_steps=["Re-run the investigation if needed"]),
        )
        return _finish(report, timeline, emit)


def _respond(alert, approver, emit, dry_run, stop, incident_id, timeline, log) -> IncidentReport:
    ledger = Ledger()
    diagnosis, ledger = investigate(alert, ledger, on_event=lambda k, d: emit(k, {"incident_id": incident_id, **d}),
                                    should_stop=stop)
    log(f"Diagnosis: {diagnosis.component} — {diagnosis.cause} (confidence {diagnosis.confidence:.2f})",
        "diagnosis", {"diagnosis": diagnosis.model_dump()})

    report = IncidentReport(
        incident_id=incident_id, alert=alert, detected_at=timeline[0].at, status="diagnosed", diagnosis=diagnosis,
        evidence=[EvidenceItem(id=e.id, tool=e.tool, args=e.args, summary=_short(e.summary))
                  for e in ledger.items() if e.id in diagnosis.evidence],
        timeline=timeline,
    )

    proposal, why = remedy.propose(diagnosis, ledger)
    if proposal is None:
        report.status = "escalated"
        log(f"Escalated: {why}", "escalated", {"reason": why})
        return _finish(report, timeline, emit)

    report.proposed_action = proposal
    log(f"Proposed: {proposal.action} on {proposal.target}. {proposal.reason}", "proposal",
        {"proposal": proposal.model_dump(), "dry_run": dry_run})

    if safety.kill_switch_engaged():
        report.status = "escalated"
        log("Kill switch engaged: no action taken", "escalated", {"reason": "kill switch"})
        return _finish(report, timeline, emit)

    approved, who = approver(proposal, report)
    if stop():
        raise Cancelled("cancelled while waiting for approval")
    report.approval = Approval(decision="approved" if approved else "rejected", by=who, at=_now())
    log(f"{'Approved' if approved else 'Rejected'} by {who}", "approval", {"approved": approved, "by": who})
    if not approved:
        report.status = "rejected"
        return _finish(report, timeline, emit)

    result = actions.execute(proposal, who, dry_run, incident_id)
    report.execution = Execution(executed=result.get("status") == "done", dry_run=dry_run,
                                 action_id=result.get("id"), result=result.get("result", ""), at=_now())
    log(f"Action {result.get('status')}: {result.get('result')}", "execution", {"result": result})
    if dry_run:
        report.status = "dry_run"
        return _finish(report, timeline, emit)
    if result.get("status") != "done":
        report.status = "failed"
        return _finish(report, timeline, emit)

    log(f"Verifying recovery of {proposal.target}", "verifying", {"target": proposal.target})
    report.verification = verify_recovery(diagnosis.component or proposal.target, should_stop=stop)
    if report.verification.recovered:
        report.status = "resolved"
        log("Recovered: back to baseline", "verified", {"recovered": True})
    else:
        undo = actions.rollback(result["id"], who)
        report.verification.rolled_back = undo.get("status") == "done"
        report.status = "escalated"
        log(f"Not recovered; rollback: {undo.get('result')}. Escalating.", "verified",
            {"recovered": False, "rollback": undo})
    return _finish(report, timeline, emit)


def _finish(report: IncidentReport, timeline: list[TimelineEntry], emit: EventSink) -> IncidentReport:
    report.timeline = list(timeline)
    report.prevention = PREVENTION.get(report.proposed_action.action if report.proposed_action else None, [])
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{report.incident_id}.json").write_text(report.model_dump_json(indent=2))
    (REPORT_DIR / f"{report.incident_id}.md").write_text(to_markdown(report))
    emit("report", {"incident_id": report.incident_id, "status": report.status, "report": report.model_dump(mode="json")})
    return report


def to_markdown(r: IncidentReport) -> str:
    d = r.diagnosis
    lines = [
        f"# Incident {r.incident_id}",
        "",
        f"**Status:** {r.status}  ",
        f"**Alert:** {r.alert}  ",
        f"**Detected:** {r.detected_at:%Y-%m-%d %H:%M:%S} UTC",
        "",
        "## Root cause",
        f"- Component: **{d.component or 'unknown'}** ({d.category})",
        f"- Cause: {d.cause}",
        f"- Confidence: {d.confidence:.2f}" + (" — escalated to a human" if d.escalate else ""),
        "",
        "## Evidence",
        *[f"- `{e.id}` ({e.tool}): {e.summary[:300]}" for e in r.evidence],
        "",
        "## Action",
    ]
    if r.proposed_action:
        a = r.proposed_action
        lines += [f"- Proposed: `{a.action}` on `{a.target}` — {a.reason}",
                  f"- Approval: {r.approval.decision}" + (f" by {r.approval.by}" if r.approval.by else "")]
        if r.execution:
            lines.append(f"- Execution: {r.execution.result}" + (" (dry run)" if r.execution.dry_run else ""))
        if r.verification:
            lines.append(f"- Recovered: {r.verification.recovered}; rolled back: {r.verification.rolled_back}")
    else:
        lines.append("- None taken")
    if d.next_steps:
        lines += ["", "## Next steps for a human", *[f"- {s}" for s in d.next_steps]]
    lines += ["", "## Timeline", *[f"- {t.at:%H:%M:%S} {t.event}" for t in r.timeline],
              "", "## Prevention", *[f"- {p}" for p in r.prevention], ""]
    return "\n".join(lines)

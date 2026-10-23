import pytest

from oncall import incident, remedy
from oncall.evidence import Ledger
from oncall.schema import Diagnosis, Verification


def _diag(component="valkey-cart", escalate=False):
    return Diagnosis(component=component, category="infrastructure", cause="stopped", confidence=0.9,
                     evidence=["ev-001"], escalate=escalate)


def _ledger(problems):
    ledger = Ledger()
    ledger.add("container_health", {}, {"with_problems": [{"name": "valkey-cart", "problems": problems}],
                                        "healthy": ["cart"]})
    return ledger


def test_stopped_container_gets_a_restart():
    action, _ = remedy.propose(_diag(), _ledger(["not running"]))
    assert action.action == "restart_service" and action.target == "valkey-cart"


def test_healthy_container_app_fault_escalates():
    action, why = remedy.propose(_diag("cart"), _ledger(["not running"]))
    assert action is None and "application" in why


def test_escalated_diagnosis_never_acts():
    action, _ = remedy.propose(_diag(escalate=True), _ledger(["not running"]))
    assert action is None


def test_traffic_flood_throttles_load():
    action, _ = remedy.propose(_diag("load-generator"), Ledger())
    assert action.action == "throttle_load_generator"


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(incident, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(incident, "investigate", lambda alert, ledger, on_event: (_diag(), _ledger(["not running"])))
    monkeypatch.setattr(incident.safety, "kill_switch_engaged", lambda: False)
    calls = []
    monkeypatch.setattr(incident.actions, "execute",
                        lambda p, who, dry, inc: calls.append(("execute", p.target, who, dry)) or
                        {"id": "act-1", "status": "dry_run" if dry else "done", "result": "ok"})
    monkeypatch.setattr(incident.actions, "rollback",
                        lambda aid, who: calls.append(("rollback", aid)) or {"status": "done", "result": "undone"})
    return calls


def test_rejected_action_changes_nothing(loop):
    report = incident.respond("alert", lambda p, r: (False, "agb"), dry_run=False)
    assert report.status == "rejected" and loop == []


def test_approved_action_runs_and_verifies(loop, monkeypatch):
    monkeypatch.setattr(incident, "verify_recovery", lambda c: Verification(recovered=True, checks=["ok"]))
    events = []
    report = incident.respond("alert", lambda p, r: (True, "agb"), lambda k, d: events.append(k), dry_run=False)
    assert report.status == "resolved" and loop == [("execute", "valkey-cart", "agb", False)]
    assert {"alert", "diagnosis", "proposal", "approval", "execution", "verified", "report"} <= set(events)
    assert len(report.timeline) >= 5


def test_failed_recovery_rolls_back_and_escalates(loop, monkeypatch):
    monkeypatch.setattr(incident, "verify_recovery", lambda c: Verification(recovered=False, checks=["still bad"]))
    report = incident.respond("alert", lambda p, r: (True, "agb"), dry_run=False)
    assert report.status == "escalated" and report.verification.rolled_back is True
    assert loop[-1] == ("rollback", "act-1")


def test_dry_run_reports_without_verifying(loop):
    report = incident.respond("alert", lambda p, r: (True, "agb"), dry_run=True)
    assert report.status == "dry_run" and loop == [("execute", "valkey-cart", "agb", True)]

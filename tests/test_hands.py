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



def _health(name, problems):
    ledger = Ledger()
    rows = {"with_problems": [{"name": name, "problems": problems}]} if problems else {"with_problems": [], "healthy": [name]}
    ledger.add("container_health", {}, rows)
    return ledger


def test_cpu_starved_container_gets_its_limit_lifted():
    action, _ = remedy.propose(_diag("product-catalog"), _health("product-catalog", ["cpu limited to 0.05 cores"]))
    assert action.action == "remove_cpu_limit" and action.target == "product-catalog"


def test_network_delay_gets_a_restart():
    action, _ = remedy.propose(_diag("cart"), _health("cart", ["network delay 2s"]))
    assert action.action == "restart_service" and action.target == "cart"


def test_surge_on_a_healthy_frontend_throttles_load():
    alert = "frontend: request rate 74.8/s (baseline 27.9/s)"
    action, _ = remedy.propose(_diag("frontend"), _health("frontend", []), alert)
    assert action.action == "throttle_load_generator" and action.params == {"users": 1}


def test_surge_with_slowness_elsewhere_is_not_a_flood():
    alert = "frontend: request rate 66.2/s (baseline 27.9/s)"
    ledger = _health("frontend", [])
    ledger.add("service_overview", {}, [{"service": "frontend-proxy", "rps": 60, "p95_ms": 2136.0, "base_p95_ms": 34.0}])
    action, _ = remedy.propose(_diag("frontend"), ledger, alert)
    assert action is None


def test_latency_on_a_healthy_service_escalates():
    action, why = remedy.propose(_diag("ad"), _health("ad", []), "ad: p95 751 ms (baseline 4 ms)")
    assert action is None and "application" in why

@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(incident, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(incident, "investigate",
                        lambda alert, ledger, on_event, should_stop=None: (_diag(), _ledger(["not running"])))
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
    monkeypatch.setattr(incident, "verify_recovery", lambda c, *a, **kw: Verification(recovered=True, checks=["ok"]))
    events = []
    report = incident.respond("alert", lambda p, r: (True, "agb"), lambda k, d: events.append(k), dry_run=False)
    assert report.status == "resolved" and loop == [("execute", "valkey-cart", "agb", False)]
    assert {"alert", "diagnosis", "proposal", "approval", "execution", "verified", "report"} <= set(events)
    assert len(report.timeline) >= 5


def test_failed_recovery_rolls_back_and_escalates(loop, monkeypatch):
    monkeypatch.setattr(incident, "verify_recovery", lambda c, *a, **kw: Verification(recovered=False, checks=["still bad"]))
    report = incident.respond("alert", lambda p, r: (True, "agb"), dry_run=False)
    assert report.status == "escalated" and report.verification.rolled_back is True
    assert loop[-1] == ("rollback", "act-1")


def test_cancel_during_investigation_reports_cancelled(loop, monkeypatch):
    from oncall.agent import Cancelled

    def cancelled(alert, ledger, on_event, should_stop=None):
        raise Cancelled("stopped")

    monkeypatch.setattr(incident, "investigate", cancelled)
    events = []
    report = incident.respond("alert", lambda p, r: (True, "agb"), lambda k, d: events.append(k), dry_run=False)
    assert report.status == "cancelled" and loop == []
    assert "cancelled" in events and "report" in events


def test_cancel_while_waiting_for_approval_changes_nothing(loop):
    stop = {"now": False}

    def approver(proposal, report):
        stop["now"] = True  # operator presses Cancel instead of answering
        return False, "cancelled"

    report = incident.respond("alert", approver, dry_run=False, should_stop=lambda: stop["now"])
    assert report.status == "cancelled" and loop == []


def test_agent_stops_before_calling_the_model(monkeypatch):
    from oncall import agent

    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    monkeypatch.setattr(agent.llm, "unload", lambda: None)
    with pytest.raises(agent.Cancelled):
        agent.investigate("alert", should_stop=lambda: True)


def test_alert_services_are_parsed():
    assert incident.alert_services("frontend: error rate 6.4% (baseline 0.0%); recommendation: error rate 12.5%") \
        == ["frontend", "recommendation"]


def test_recovery_waits_for_container_and_alerting_services(monkeypatch):
    states = iter([(False, "astronomy-db: exited"), (True, "astronomy-db: running"), (True, "astronomy-db: running")])
    anomalies = iter([[], [{"service": "frontend", "detail": "error rate 6%"}], []])
    monkeypatch.setattr(incident, "_container_ok", lambda name: next(states))
    monkeypatch.setattr(incident.detector, "current_anomalies", lambda: next(anomalies))
    monkeypatch.setattr(incident.time, "sleep", lambda s: None)
    v = incident.verify_recovery("astronomy-db", ["frontend"], timeout_s=600, every_s=1)
    assert v.recovered is True and len(v.checks) == 3


def test_recovery_check_survives_metrics_outage(monkeypatch):
    calls = iter([incident.detector.MetricsUnavailable("timeout"), []])

    def anomalies():
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(incident, "_container_ok", lambda name: (True, f"{name}: running"))
    monkeypatch.setattr(incident.detector, "current_anomalies", anomalies)
    monkeypatch.setattr(incident.time, "sleep", lambda s: None)
    v = incident.verify_recovery("cart", [], timeout_s=600, every_s=1)
    assert v.recovered is True and "metrics unavailable" in v.checks[0]


def test_dry_run_reports_without_verifying(loop):
    report = incident.respond("alert", lambda p, r: (True, "agb"), dry_run=True)
    assert report.status == "dry_run" and loop == [("execute", "valkey-cart", "agb", True)]

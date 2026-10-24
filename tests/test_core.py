import pytest

from oncall import tools
from oncall.agent import _finalise
from oncall.evidence import Ledger
from oncall.http import ForbiddenEndpoint, check_allowed
from oncall.safety import ActionBlocked, ProposedAction, ask_approval


def test_ledger_flags_invented_citations():
    ledger = Ledger()
    real = ledger.add("service_overview", {}, {"checkout": "errors"})
    assert real.id == "ev-001"
    assert ledger.missing(["ev-001", "ev-999"]) == ["ev-999"]


def test_diagnosis_with_invented_evidence_escalates():
    ledger = Ledger()
    ledger.add("service_overview", {}, {})
    reply = '{"component": "payment", "cause": "x", "confidence": 0.9, "evidence": ["ev-007"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6).escalate is True


OVERVIEW_PAYMENT_BAD = [{"service": "payment", "rps": 0.1, "error_pct": 60.0, "p95_ms": 40.0},
                        {"service": "checkout", "rps": 0.5, "error_pct": 0.0, "p95_ms": 50.0}]
LOGS_PAYMENT = {"distinct_messages": [{"service": "payment", "count": 9, "example": "charge failed"}]}


def test_single_tool_evidence_is_not_trusted():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_PAYMENT_BAD)
    reply = '{"component": "payment", "cause": "x", "confidence": 0.95, "evidence": ["ev-001"], "escalate": false}'
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is True and d.confidence <= 0.5


def test_corroborated_diagnosis_is_accepted():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_PAYMENT_BAD)
    ledger.add("search_logs", {"service": "payment"}, LOGS_PAYMENT)
    reply = ('{"component": "payment", "cause": "x", "confidence": 0.9, '
             '"evidence": ["ev-001", "ev-002"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is False and d.confidence == 0.9


def test_healthy_mentions_do_not_corroborate():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_PAYMENT_BAD)  # checkout listed, but healthy
    ledger.add("container_health", {}, {"with_problems": [], "healthy": ["checkout", "payment"]})
    reply = '{"component": "checkout", "cause": "x", "confidence": 1.0, "evidence": ["ev-001", "ev-002"], "escalate": false}'
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is True and d.confidence <= 0.5


def test_uncited_supporting_evidence_counts_and_is_cited():
    ledger = Ledger()
    ledger.add("search_traces", {}, {"failure_origins": [{"service": "cart", "calls": "valkey-cart", "count": 5}]})
    ledger.add("container_health", {}, {"with_problems": [{"name": "valkey-cart", "problems": ["not running"]}]})
    reply = ('{"component": "valkey-cart", "cause": "x", "confidence": 0.9, '
             '"evidence": ["ev-002"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is False and sorted(d.evidence) == ["ev-001", "ev-002"]


ALL_CONTAINERS_CACHE_DOWN = {"with_problems": [{"name": "valkey-cart", "problems": ["not running"]}],
                             "healthy": ["cart", "checkout"]}
OVERVIEW_CART_ERRORS = [{"service": "cart", "rps": 3.0, "error_pct": 45.0, "p95_ms": 15000.0}]


def test_stopped_container_overrides_a_symptom_diagnosis():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_CART_ERRORS)
    ledger.add("container_health", {}, ALL_CONTAINERS_CACHE_DOWN)
    reply = '{"component": "cart", "cause": "cart errors", "confidence": 1.0, "evidence": ["ev-001"], "escalate": false}'
    d = _finalise(reply, ledger, 0.6)
    assert d.component == "valkey-cart" and d.category == "infrastructure"
    assert d.escalate is False and "ev-002" in d.evidence


def test_unanswered_callee_overrides_the_caller(monkeypatch):
    from oncall import agent
    monkeypatch.setattr(agent, "_known_components", lambda ledger: ["checkout", "payment"])
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "checkout", "rps": 0.5, "error_pct": 40.0, "p95_ms": 900.0}])
    ledger.add("search_traces", {"service": "checkout"},
               {"failure_origins": [{"service": "checkout", "calls": "payment", "count": 9,
                                     "note": "no response recorded from payment; suspect the callee"}]})
    ledger.add("container_health", {}, {"with_problems": [], "healthy": ["checkout", "payment"]})
    reply = ('{"component": "checkout", "cause": "x", "confidence": 1.0, '
             '"evidence": ["ev-001", "ev-002"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6)
    assert d.component == "payment" and d.escalate is False  # caller errors corroborate the silent callee


def test_stopped_container_found_by_name_and_alert_corroborate():
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "frontend", "rps": 26.0, "error_pct": 0.6, "p95_ms": 290.0}])
    ledger.add("container_health", {"name": "checkout"},
               {"containers": [{"name": "checkout", "state": "exited", "problems": ["not running", "unhealthy"]}]})
    reply = '{"component": "checkout", "cause": "stopped", "confidence": 0.9, "evidence": ["ev-002"], "escalate": false}'
    d = _finalise(reply, ledger, 0.6, alert="checkout: no requests (baseline 0.6/s)")
    assert d.component == "checkout" and d.escalate is False
    assert d.category == "infrastructure"  # the model left it out; the container evidence decides


@pytest.mark.parametrize("problems, category", [
    (["cpu limited to 0.05 cores"], "infrastructure"),
    (["network delay 2s"], "infrastructure"),
    (["high cpu"], "unknown"),  # could be the application's own fault: left as the model said
])
def test_category_follows_the_container_evidence(problems, category):
    ledger = Ledger()
    ledger.add("container_health", {}, {"with_problems": [{"name": "cart", "problems": problems}], "healthy": []})
    reply = '{"component": "cart", "cause": "slow", "confidence": 0.9, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6, alert="cart: p95 4000 ms (baseline 2 ms)").category == category


def test_lone_troubled_container_replaces_an_unsupported_pick():
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "frontend", "rps": 26.0, "error_pct": 0.0, "p95_ms": 300.0}])
    ledger.add("container_health", {}, {"with_problems": [{"name": "ad", "problems": ["high cpu"]}],
                                        "healthy": ["frontend", "cart"]})
    reply = '{"component": "frontend", "cause": "x", "confidence": 0.9, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6).component == "ad"


def test_failure_start_downstream_beats_the_alerting_caller(monkeypatch):
    from oncall import agent
    monkeypatch.setattr(agent, "_known_components", lambda ledger: ["checkout", "cart", "frontend"])
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "checkout", "rps": 0.6, "error_pct": 2.5, "p95_ms": 45.0}])
    ledger.add("search_traces", {"service": "checkout"}, {
        "failure_origins": [{"service": "cart", "operation": "EmptyCart", "count": 2,
                             "error": "Can't access cart storage"}],
        "calls": ["checkout→cart", "frontend→checkout"]})
    ledger.add("search_logs", {"service": "cart"}, {"distinct_messages": [{"service": "cart", "count": 3,
                                                                           "example": "Wasn't able to connect"}]})
    ledger.add("container_health", {}, {"with_problems": [], "healthy": ["cart", "checkout"]})
    reply = ('{"component": "checkout", "cause": "slow", "confidence": 0.95, '
             '"evidence": ["ev-001"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6, alert="checkout: p95 1020 ms (baseline 30 ms)")
    assert d.component == "cart" and d.escalate is False


def test_failure_start_is_ignored_without_a_call_path(monkeypatch):
    from oncall import agent
    monkeypatch.setattr(agent, "_known_components", lambda ledger: ["checkout", "ad"])
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "checkout", "rps": 0.6, "error_pct": 30.0, "p95_ms": 45.0}])
    ledger.add("search_traces", {}, {"failure_origins": [{"service": "ad", "count": 9, "error": "boom"}],
                                     "calls": ["frontend→ad"]})
    reply = '{"component": "checkout", "cause": "x", "confidence": 0.9, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6, alert="checkout: error rate 30%").component == "checkout"


def test_recent_restart_alone_does_not_override():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_CART_ERRORS)
    ledger.add("container_health", {}, {"with_problems": [{"name": "ad", "problems": ["restarted recently"]}]})
    reply = '{"component": "cart", "cause": "x", "confidence": 1.0, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6).component == "cart"


def test_not_running_beats_recently_restarted():
    ledger = Ledger()
    ledger.add("service_overview", {}, OVERVIEW_CART_ERRORS)
    ledger.add("container_health", {}, {"with_problems": [
        {"name": "ad", "problems": ["restarted recently"]},
        {"name": "astronomy-db", "problems": ["not running"]}]})
    reply = '{"component": "cart", "cause": "x", "confidence": 1.0, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6).component == "astronomy-db"


def test_load_generator_is_never_corroborated():
    ledger = Ledger()
    ledger.add("service_overview", {}, [{"service": "load-generator", "rps": 1.5, "error_pct": 40.0, "p95_ms": 9000.0}])
    ledger.add("container_health", {}, {"with_problems": [{"name": "load-generator", "problems": ["high cpu"]}]})
    reply = ('{"component": "load-generator", "cause": "x", "confidence": 1.0, '
             '"evidence": ["ev-001", "ev-002"], "escalate": false}')
    assert _finalise(reply, ledger, 0.6).escalate is True


def test_final_schema_limits_component_to_real_names():
    from oncall.agent import _final_schema
    schema = _final_schema(["cart", "valkey-cart"])
    assert schema["properties"]["component"]["anyOf"][0]["enum"] == ["cart", "valkey-cart"]


def test_low_confidence_escalates():
    ledger = Ledger()
    ledger.add("service_overview", {}, {})
    reply = '{"component": "cart", "cause": "x", "confidence": 0.3, "evidence": ["ev-001"], "escalate": false}'
    assert _finalise(reply, ledger, 0.6).escalate is True


@pytest.mark.parametrize("url", ["http://flagd:8013/x", "http://localhost:8080/feature/", "http://localhost:32771/flagd"])
def test_flag_endpoints_are_blocked(url):
    with pytest.raises(ForbiddenEndpoint):
        check_allowed(url)


def test_unknown_tool_is_reported_not_raised():
    assert "error" in tools.call("read_flag_settings", {}, Ledger())


def test_non_allowlisted_action_is_blocked():
    with pytest.raises(ActionBlocked):
        ask_approval(ProposedAction("delete_database", "astronomy-db", "x"), ask=lambda _: "yes")


def test_only_explicit_yes_approves():
    proposal = ProposedAction("restart_service", "product-catalog", "health check failing")
    assert ask_approval(proposal, ask=lambda _: "yes") is True
    assert ask_approval(proposal, ask=lambda _: "y") is False

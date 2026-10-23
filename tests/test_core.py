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


def test_single_tool_evidence_is_not_trusted():
    ledger = Ledger()
    ledger.add("service_overview", {}, {"payment": "errors"})
    reply = '{"component": "payment", "cause": "x", "confidence": 0.95, "evidence": ["ev-001"], "escalate": false}'
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is True and d.confidence <= 0.5


def test_corroborated_diagnosis_is_accepted():
    ledger = Ledger()
    ledger.add("service_overview", {}, {"payment": "100% errors"})
    ledger.add("search_logs", {"service": "payment"}, {"payment": "charge failed"})
    reply = ('{"component": "payment", "cause": "x", "confidence": 0.9, '
             '"evidence": ["ev-001", "ev-002"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is False and d.confidence == 0.9


def test_uncited_supporting_evidence_counts_and_is_cited():
    ledger = Ledger()
    ledger.add("search_traces", {}, {"origin": "valkey-cart did not answer"})
    ledger.add("container_health", {}, {"name": "valkey-cart", "state": "exited"})
    reply = ('{"component": "valkey-cart", "cause": "x", "confidence": 0.9, '
             '"evidence": ["ev-002"], "escalate": false}')
    d = _finalise(reply, ledger, 0.6)
    assert d.escalate is False and sorted(d.evidence) == ["ev-001", "ev-002"]


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

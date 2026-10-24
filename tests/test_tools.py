from oncall.tools.containers import _pct
from oncall.tools.logs import _pattern
from oncall.tools.traces import _attrs, _origins


def _span(span_id, parent, error, service="svc", name="op"):
    return {"trace_id": "t1", "span_id": span_id, "parent_id": parent, "service": service, "kind": "server",
            "callee": "", "name": name, "error": error, "message": "", "ms": 1.0}


def test_failure_origin_is_deepest_failing_span():
    spans = [
        _span("a", "", True, "frontend"),
        _span("b", "a", True, "checkout"),
        _span("c", "b", True, "payment"),
        _span("d", "b", False, "currency"),
    ]
    assert [s["service"] for s in _origins(spans)] == ["payment"]


def test_feature_flag_attributes_are_stripped():
    attrs = [
        {"key": "feature_flag.key", "value": {"stringValue": "secret"}},
        {"key": "http.status_code", "value": {"intValue": "500"}},
    ]
    assert _attrs(attrs) == {"http.status_code": "500"}


def test_log_pattern_groups_variable_parts():
    a = _pattern("order 12345 failed for user 9f8e7d6c5b4a")
    b = _pattern("order 999 failed for user 0a1b2c3d4e5f")
    assert a == b


def test_failures_to_undeployed_hosts_are_ignored(monkeypatch):
    from oncall.tools import traces
    spans = [dict(_span("a", "", True, "load-generator", "POST"),
                  message="ConnectionError: HTTPConnectionPool(host='agent', port=8010)"),
             dict(_span("b", "", True, "frontend", "GET"), message="upstream failed")]
    monkeypatch.setattr(traces, "fetch_spans", lambda service, minutes: spans)
    monkeypatch.setattr(traces, "_deployed", lambda: {"frontend", "cart"})
    origins = traces.search_traces("frontend")["failure_origins"]
    assert [o["service"] for o in origins] == ["frontend"]


def test_callee_from_rpc_span_name():
    from oncall.tools.traces import _service_from_rpc
    assert _service_from_rpc("oteldemo.PaymentService/Charge") == "payment"
    assert _service_from_rpc("oteldemo.ProductCatalogService/GetProduct") == "product-catalog"
    assert _service_from_rpc("GET /api/cart") == ""


def test_percent_parsing():
    assert _pct("93.5%") == 93.5
    assert _pct("--") == 0.0

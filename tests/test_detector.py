from oncall.detector import alert_text, anomalies

BASE = {
    "checkout": {"service": "checkout", "rps": 0.5, "error_pct": 0.0, "p95_ms": 60.0},
    "cart": {"service": "cart", "rps": 5.0, "error_pct": 0.0, "p95_ms": 2.0},
    "load-generator": {"service": "load-generator", "rps": 1.6, "error_pct": 2.0, "p95_ms": 4000.0},
}


def test_healthy_metrics_raise_nothing():
    assert anomalies(BASE, BASE) == []


def test_error_jump_fires():
    now = {**BASE, "checkout": {**BASE["checkout"], "error_pct": 40.0}}
    found = anomalies(BASE, now)
    assert [(a["service"], a["kind"]) for a in found] == [("checkout", "errors")]


def test_small_error_blip_is_ignored():
    now = {**BASE, "checkout": {**BASE["checkout"], "error_pct": 3.0}}
    assert anomalies(BASE, now) == []


def test_silent_service_fires():
    now = {k: v for k, v in BASE.items() if k != "cart"}
    assert anomalies(BASE, now)[0]["kind"] == "silent"


def test_latency_jump_fires_but_load_generator_is_ignored():
    now = {**BASE,
           "cart": {**BASE["cart"], "p95_ms": 2500.0},
           "load-generator": {**BASE["load-generator"], "error_pct": 60.0}}
    found = anomalies(BASE, now)
    assert [(a["service"], a["kind"]) for a in found] == [("cart", "latency")]
    assert "cart" in alert_text(found)

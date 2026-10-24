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


def test_few_errors_on_a_quiet_service_are_noise():
    base = {"payment": {"service": "payment", "rps": 0.07, "error_pct": 6.7, "p95_ms": 40.0}}
    now = {"payment": {"service": "payment", "rps": 0.07, "error_pct": 14.3, "p95_ms": 40.0}}  # ~2 failures
    assert anomalies(base, now) == []


def test_real_payment_failure_on_a_quiet_service_fires():
    base = {"payment": {"service": "payment", "rps": 0.07, "error_pct": 6.7, "p95_ms": 40.0}}
    now = {"payment": {"service": "payment", "rps": 0.07, "error_pct": 60.0, "p95_ms": 40.0}}  # ~8 failures
    assert anomalies(base, now)[0]["kind"] == "errors"


def test_traffic_surge_fires():
    base = {"frontend": {"service": "frontend", "rps": 27.0, "error_pct": 0.0, "p95_ms": 35.0}}
    now = {"frontend": {"service": "frontend", "rps": 69.0, "error_pct": 0.0, "p95_ms": 21.0}}
    assert anomalies(base, now)[0]["kind"] == "surge"


def test_moderate_latency_jump_on_a_fast_service_fires():
    base = {"ad": {"service": "ad", "rps": 0.6, "error_pct": 0.0, "p95_ms": 4.0}}
    now = {"ad": {"service": "ad", "rps": 0.5, "error_pct": 0.0, "p95_ms": 766.0}}
    assert anomalies(base, now)[0]["kind"] == "latency"


def test_small_error_blip_is_ignored():
    now = {**BASE, "checkout": {**BASE["checkout"], "error_pct": 3.0}}
    assert anomalies(BASE, now) == []


BUSY = {f"svc{i}": {"service": f"svc{i}", "rps": 2.0, "error_pct": 0.0, "p95_ms": 10.0} for i in range(5)}


def test_silent_service_fires():
    now = {k: v for k, v in BUSY.items() if k != "svc1"}
    found = anomalies(BUSY, now)
    assert [(a["service"], a["kind"]) for a in found] == [("svc1", "silent")]


def test_many_silent_services_is_a_telemetry_gap_not_an_alert():
    now = {k: v for k, v in BUSY.items() if k in ("svc0", "svc1")}  # 3 of 5 missing
    assert anomalies(BUSY, now) == []


def test_quiet_low_traffic_service_is_not_silent():
    base = {**BUSY, "checkout": {"service": "checkout", "rps": 0.2, "error_pct": 0.0, "p95_ms": 50.0}}
    assert anomalies(base, BUSY) == []


def test_silence_must_hold_in_the_longer_window():
    now = {k: v for k, v in BUSY.items() if k != "svc1"}
    assert anomalies(BUSY, now, recent=BUSY) == []  # still seen in the 5-minute window


def test_latency_jump_fires_but_load_generator_is_ignored():
    now = {**BASE,
           "cart": {**BASE["cart"], "p95_ms": 2500.0},
           "load-generator": {**BASE["load-generator"], "error_pct": 60.0}}
    found = anomalies(BASE, now)
    assert [(a["service"], a["kind"]) for a in found] == [("cart", "latency")]
    assert "cart" in alert_text(found)

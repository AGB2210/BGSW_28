import pytest

from faultctl import incidents


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(incidents, "FLAG_BACKUP", tmp_path / "flags.json")
    log = []
    flags_now = {"paymentFailure": "off"}

    def set_variant(flag, variant):
        previous, flags_now[flag] = flags_now[flag], variant
        log.append(("flag", flag, variant))
        return previous

    monkeypatch.setattr(incidents.flags, "set_variant", set_variant)
    monkeypatch.setattr(incidents.flags, "backup", lambda dest: None)
    monkeypatch.setattr(incidents.docker, "stop", lambda c: log.append(("stop", c)))
    monkeypatch.setattr(incidents.docker, "start", lambda c: log.append(("start", c)))
    monkeypatch.setattr(incidents.docker, "nano_cpus", lambda c: 0)
    monkeypatch.setattr(incidents.docker, "set_cpus", lambda c, n: log.append(("cpus", c, n)))
    return log, flags_now


def test_flag_incident_is_undone_by_reset(fake):
    log, flags_now = fake
    incidents.inject("payment-failure")
    assert flags_now["paymentFailure"] == "50%"
    incidents.reset()
    assert flags_now["paymentFailure"] == "off"
    assert incidents.load_state() == {"active": []}


def test_infra_incidents_record_and_reverse(fake):
    log, _ = fake
    incidents.inject("service-down", target="cart")
    incidents.inject("cpu-starve")
    incidents.reset()
    assert log == [("stop", "cart"), ("cpus", "product-catalog", 0.05),
                   ("cpus", "product-catalog", 0.0), ("start", "cart")]


def test_network_delay_is_removed_by_reset(fake, monkeypatch):
    log, _ = fake
    monkeypatch.setattr(incidents.docker, "add_delay", lambda c, ms: log.append(("delay", c, ms)))
    monkeypatch.setattr(incidents.docker, "clear_delay", lambda c: log.append(("clear", c)))
    incidents.inject("network-latency")
    incidents.reset()
    assert log == [("delay", "cart", 2000), ("clear", "cart")]


def test_unknown_incident_raises(fake):
    with pytest.raises(KeyError):
        incidents.inject("does-not-exist")

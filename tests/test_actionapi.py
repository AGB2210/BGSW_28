import pytest

from actionapi import actions


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "LOG_FILE", tmp_path / "actions.jsonl")
    monkeypatch.setattr(actions, "KILL_SWITCH", tmp_path / ".killswitch")
    monkeypatch.setattr(actions.docker, "demo_containers", lambda: ["cart", "checkout", "jaeger"])
    calls = []
    monkeypatch.setattr(actions.docker, "is_running", lambda name: True)
    monkeypatch.setattr(actions.docker, "restart", lambda name: calls.append(("restart", name)))
    monkeypatch.setattr(actions.flags, "set_variant", lambda flag, v: calls.append(("flag", flag, v)) or "on")
    monkeypatch.setattr(actions.docker, "nano_cpus", lambda name: 50_000_000 if name == "cart" else 0)
    monkeypatch.setattr(actions.docker, "set_cpus", lambda name, cpus: calls.append(("cpus", name, cpus)))
    return calls


def req(**kw):
    return {"action": "restart_service", "target": "cart", "reason": "test", "dry_run": False, "approved_by": "agb", **kw}


def test_unknown_action_is_refused_and_logged():
    entry = actions.execute(req(action="delete_database"))
    assert entry["status"] == "refused"
    assert actions.history()[-1]["id"] == entry["id"]


def test_real_action_needs_an_approver(isolated):
    assert actions.execute(req(approved_by=None))["status"] == "refused"
    assert isolated == []


def test_dry_run_changes_nothing(isolated):
    entry = actions.execute(req(dry_run=True, approved_by=None))
    assert entry["status"] == "dry_run" and isolated == []


def test_kill_switch_blocks_real_actions(isolated):
    actions.KILL_SWITCH.touch()
    assert actions.execute(req())["status"] == "refused"
    assert isolated == []


def test_observability_stack_is_protected(isolated):
    assert actions.execute(req(target="jaeger"))["status"] == "refused"
    assert isolated == []


def test_approved_restart_runs(isolated):
    assert actions.execute(req())["status"] == "done"
    assert isolated == [("restart", "cart")]


def test_flag_toggle_can_be_rolled_back(isolated):
    done = actions.execute(req(action="toggle_feature_flag", target="someFlag", params={"variant": "off"}))
    assert done["undo"] == {"previous_variant": "on"}
    back = actions.rollback(done["id"], "agb")
    assert back["status"] == "done"
    assert isolated[-1] == ("flag", "someFlag", "on")


def test_clear_archives_the_log_and_rollback_still_works(isolated):
    done = actions.execute(req(action="toggle_feature_flag", target="someFlag", params={"variant": "off"}))
    archived = actions.clear()
    assert actions.history() == [] and (actions.LOG_FILE.parent / archived).exists()
    assert actions.rollback(done["id"], "agb")["status"] == "done"
    assert actions.clear() is not None and actions.clear() is None  # the rollback entry, then nothing


def test_scale_is_refused_on_compose():
    assert actions.execute(req(action="scale_service"))["status"] == "refused"


def test_cpu_limit_is_lifted_and_can_be_put_back(isolated):
    done = actions.execute(req(action="remove_cpu_limit"))
    assert done["status"] == "done" and isolated == [("cpus", "cart", 0)]
    assert actions.rollback(done["id"], "agb")["status"] == "done"
    assert isolated[-1] == ("cpus", "cart", 0.05)


def test_cpu_limit_action_needs_a_limit_and_spares_the_telemetry_stack(isolated):
    assert actions.execute(req(action="remove_cpu_limit", target="checkout"))["status"] == "refused"
    assert actions.execute(req(action="remove_cpu_limit", target="jaeger"))["status"] == "refused"
    assert isolated == []


def test_throttle_rollback_restores_the_default_load_by_restarting(isolated, monkeypatch):
    monkeypatch.setattr(actions.loadgen, "user_count", lambda: 5)
    monkeypatch.setattr(actions.loadgen, "set_users", lambda users, spawn_rate=1.0: isolated.append(("users", users)))
    done = actions.execute(req(action="throttle_load_generator", target="load-generator", params={"users": 1}))
    assert actions.rollback(done["id"], "agb")["status"] == "done"
    assert isolated == [("users", 1), ("restart", "load-generator")]  # a restart brings the browser user back

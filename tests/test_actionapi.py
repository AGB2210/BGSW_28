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


def test_scale_is_refused_on_compose():
    assert actions.execute(req(action="scale_service"))["status"] == "refused"

# Author: Tom Sapletta · Part of the ifURI solution.
"""The node-side management contract: state, rebuild, restart (detached), atomic upgrade,
rollback — with injectable runners so nothing actually restarts a real service in tests."""
import subprocess

from urirun_connector_nodeadmin import ops
import urirun_connector_nodeadmin as na


def _fake_runner(rc=0, out="", err=""):
    def run(argv, timeout=300.0):
        return subprocess.CompletedProcess(argv, rc, out, err)
    return run


def test_runtime_state_reports_install_dir_and_version(tmp_path, monkeypatch):
    monkeypatch.setenv("URIRUN_NODE_DIR", str(tmp_path))
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "registry.json").write_text("{}")
    (tmp_path / "manifest.lock.json").write_text('{"release_id": "v1", "urirun": {"version": "0.4.194"}}')
    st = ops.runtime_state(tmp_path)
    assert st["install_dir"] == str(tmp_path) and st["registry_present"] is True
    assert st["lock"]["release_id"] == "v1"


def test_rebuild_registry_needs_bindings(tmp_path):
    r = ops.rebuild_registry(tmp_path, runner=_fake_runner())
    assert r["ok"] is False and "no bindings" in r["error"]


def test_rebuild_registry_compiles_when_bindings_present(tmp_path):
    (tmp_path / "bindings.v2.json").write_text('{"version":"urirun.bindings.v2","bindings":{}}')
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    r = ops.rebuild_registry(tmp_path, runner=_fake_runner(rc=0))
    assert r["ok"] is True and r["registry"].endswith("registry.json")


def test_restart_command_stops_then_starts_detached(tmp_path):
    cmd = ops.restart_command(tmp_path, delay=1.0)
    joined = " ".join(cmd)
    assert "pkill -f 'urirun node serve'" in joined and "run-node.sh" in joined and "nohup" in joined


def test_restart_refuses_without_runner(tmp_path):
    assert ops.restart(tmp_path)["ok"] is False  # no run-node.sh


def test_restart_spawns_detached_when_runner_present(tmp_path):
    (tmp_path / "run-node.sh").write_text("#!/bin/bash\n"); spawned = []
    r = ops.restart(tmp_path, spawn=spawned.append)
    assert r["ok"] and r["restarting"] and spawned  # the detached cmd was handed to spawn


def test_upgrade_is_atomic_keeps_current_on_smoke_fail(tmp_path):
    # a fresh install dir; build succeeds but smoke fails → current must not switch
    (tmp_path / "run-node.sh").write_text("#!/bin/bash\n")
    r = ops.upgrade("v2", root=tmp_path, runner=_fake_runner(rc=0), smoke=lambda rel: False)
    assert r["ok"] is False and r.get("switched") is False


def test_upgrade_switches_and_restarts_on_smoke_pass(tmp_path):
    (tmp_path / "run-node.sh").write_text("#!/bin/bash\n")
    import subprocess as sp
    monkey = []
    orig = ops.subprocess.Popen
    ops.subprocess.Popen = lambda c, **k: monkey.append(c)  # don't really spawn
    try:
        r = ops.upgrade("v2", root=tmp_path, runner=_fake_runner(rc=0), smoke=lambda rel: True)
    finally:
        ops.subprocess.Popen = orig
    assert r["ok"] and r["switched"] and (tmp_path / "current").resolve().name == "v2"
    assert r["restart"]["ok"]


def test_connector_exposes_the_management_routes():
    text = str(na.urirun_bindings())
    for route in ("runtime/query/state", "registry/command/rebuild", "runtime/command/restart",
                  "runtime/command/upgrade", "runtime/command/rollback", "worker/command/reload",
                  "smoke/command/run"):
        assert route in text

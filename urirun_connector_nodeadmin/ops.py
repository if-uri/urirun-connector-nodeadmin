# Author: Tom Sapletta · Part of the ifURI solution.
"""Node-side management operations — the backend the fleet's node:// remedies call.

Pure-ish operations separated from the connector envelope so they are testable: runners
(pip, urirun compile, service restart) are injectable. Restart/upgrade are the hard part
— a process cannot cleanly restart itself, so restart spawns a DETACHED helper that
replaces the service after the HTTP response has been sent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)  # noqa: S603


def install_dir() -> Path:
    return Path(os.environ.get("URIRUN_NODE_DIR") or "~/.urirun-node").expanduser()


def _venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


# --- runtime/query/state ---------------------------------------------------------
def runtime_state(root: Path | None = None) -> dict[str, Any]:
    """Rich actual-state for the fleet: version, python, connectors, registry etag.

    This is what ``actual_state.probe`` reads as the node's ``state`` — the ground truth
    a host diffs against desired state (lockfile-grade, minus git sha which node.sh writes)."""
    root = root or install_dir()
    py = _venv_python(root)
    state: dict[str, Any] = {"install_dir": str(root), "python": str(py)}
    try:
        state["urirun_version"] = _run([str(py), "-c",
            "import importlib.metadata as m;print(m.version('urirun'))"]).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        state["urirun_version"] = None
        state["error"] = str(exc)
    lock = root / "current" / "manifest.lock.json"
    if not lock.is_file():
        lock = root / "manifest.lock.json"
    if lock.is_file():
        try:
            state["lock"] = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    reg = root / "registry.json"
    state["registry_present"] = reg.is_file()
    return state


# --- registry/command/rebuild ----------------------------------------------------
def rebuild_registry(root: Path | None = None, runner: Runner = _run) -> dict[str, Any]:
    """Recompile registry.json from bindings.v2.json — fixes a registry compiled from
    stale bindings (e.g. a bad navigate→dom_click route) without touching the venv."""
    root = root or install_dir()
    bindings, registry = root / "bindings.v2.json", root / "registry.json"
    if not bindings.is_file():
        return {"ok": False, "error": f"no bindings at {bindings}"}
    cp = runner([str(_venv_python(root)), "-m", "urirun.v2", "compile",
                 str(bindings), "--out", str(registry)])
    return {"ok": cp.returncode == 0, "returncode": cp.returncode,
            "stderr": (cp.stderr or "")[-400:], "registry": str(registry)}


# --- runtime/command/restart & worker/command/reload -----------------------------
def restart_command(root: Path | None = None, delay: float = 1.0) -> list[str]:
    """The detached restart command: stop stale nodes, then start the current runner.
    Returned (not run) so it is testable; ``restart`` spawns it detached."""
    root = root or install_dir()
    runner = root / "run-node.sh"
    return ["bash", "-lc",
            f"sleep {delay}; pkill -f 'urirun node serve' 2>/dev/null || true; "
            f"sleep 1; nohup {str(runner)!r} > {str(root / 'node.log')!r} 2>&1 &"]


def restart(root: Path | None = None, spawn: Callable[[list[str]], Any] | None = None) -> dict[str, Any]:
    """Restart the node service AFTER this response returns — drops stale warm workers.
    The restart runs in a detached process so it survives this process being killed."""
    root = root or install_dir()
    cmd = restart_command(root)
    if not (root / "run-node.sh").is_file():
        return {"ok": False, "error": f"no runner at {root / 'run-node.sh'}"}
    _spawn = spawn or (lambda c: subprocess.Popen(c, start_new_session=True))  # noqa: S603
    _spawn(cmd)
    return {"ok": True, "restarting": True, "detail": "service restart scheduled (detached)"}


# --- policy/command/allow: unblock a scheme by adding it to the serve allow-list -----
def add_allow(glob: str, root: Path | None = None, runner: Runner = _run) -> dict[str, Any]:
    """Add ``glob`` (e.g. app://**) to the node's serve allow-list by rewriting the runner
    and restarting. This is the simple, remote unblock for a default-deny scheme — no
    reinstall, no file editing by hand. The block was the node's own allow-list; this edits it."""
    root = root or install_dir()
    run_sh = root / "run-node.sh"
    if not run_sh.is_file():
        return {"ok": False, "error": f"no runner at {run_sh}"}
    text = run_sh.read_text(encoding="utf-8")
    flag = f"--allow {glob!r}".replace('"', "'")
    if glob in text:
        return {"ok": True, "already": True, "glob": glob}
    lines = []
    for ln in text.splitlines():
        if "node serve" in ln and "--allow " + repr(glob) not in ln:
            ln = ln.rstrip() + f" --allow '{glob}'"
        lines.append(ln)
    run_sh.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "glob": glob, "restart": restart(root)}


# --- runtime/command/upgrade & rollback (atomic releases) ------------------------
def upgrade(release_id: str, *, root: Path | None = None, spec: str = "urirun",
            connectors: list[str] | None = None, runner: Runner = _run,
            smoke: Callable[[Path], bool] | None = None) -> dict[str, Any]:
    """Atomic upgrade: build a NEW release (fresh venv + deps + registry) off to the side,
    smoke it, and only then flip ``current`` and restart. A failed build/smoke leaves the
    running node untouched. Reuses urirun-fleet's ReleaseManager for the atomic switch."""
    root = root or install_dir()
    try:
        from urirun_fleet.rollout import ReleaseManager, deploy_release
    except ImportError as exc:
        return {"ok": False, "error": f"needs urirun-fleet for atomic releases: {exc}"}

    def _build(rel: Path) -> None:
        venv = rel / ".venv"
        runner([sys.executable, "-m", "venv", str(venv)])
        pip = venv / "bin" / "pip"
        pkgs = [spec, *[f"urirun-connector-{c}" for c in (connectors or [])]]
        runner([str(pip), "install", "--upgrade", *pkgs], )  # type: ignore[arg-type]

    def _smoke(rel: Path) -> bool:
        if smoke:
            return smoke(rel)
        py = rel / ".venv" / "bin" / "python"
        return py.is_file() and runner([str(py), "-c", "import urirun"]).returncode == 0

    mgr = ReleaseManager(root)
    res = deploy_release(mgr, release_id, build=_build, smoke=_smoke,
                         lock={"release_id": release_id, "spec": spec, "connectors": connectors or []})
    if res.get("switched"):
        res["restart"] = restart(root)
    return {"ok": bool(res.get("ok")), **res}


def rollback(root: Path | None = None) -> dict[str, Any]:
    root = root or install_dir()
    try:
        from urirun_fleet.rollout import ReleaseManager
    except ImportError as exc:
        return {"ok": False, "error": f"needs urirun-fleet: {exc}"}
    try:
        res = ReleaseManager(root).rollback()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    res["restart"] = restart(root)
    return {"ok": True, **res}

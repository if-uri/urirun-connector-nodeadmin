# Author: Tom Sapletta · Part of the ifURI solution.
"""urirun-connector-nodeadmin — the node-side management surface the fleet drives.

Serves the ``node://`` management routes a fleet reconcile plan calls: rich runtime
state, registry rebuild, restart (drop stale workers), atomic upgrade/rollback, and a
capability smoke. These are ADMIN operations — the node must gate node:// behind an
enrolled key / token (this connector assumes the node's admin gate is already in front).

Built to URI_NATIVE_CONNECTOR_CHECKLIST: lazy imports, handlers never raise (urirun
envelope), queries in-process, mutating ops isolated.
"""
from __future__ import annotations

from typing import Any

import urirun

CONNECTOR_ID = "nodeadmin"
conn = urirun.connector(CONNECTOR_ID, scheme="node")


def _ok(**kw: Any) -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, **kw)


def _fail(msg: str, action: str, **extra: Any) -> dict[str, Any]:
    return urirun.fail(msg, connector=CONNECTOR_ID, action=action, **extra)


@conn.handler("runtime/query/state", isolated=False,
              meta={"label": "Rich node runtime state (version, python, registry, lockfile) for fleet diffing"})
def runtime_state() -> dict[str, Any]:
    """The ground-truth actual-state a host diffs against desired — version, venv,
    registry presence, and the manifest lockfile when present."""
    from .ops import runtime_state as _state
    try:
        return _ok(action="node-state", state=_state())
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "node-state")


@conn.handler("registry/command/rebuild", isolated=True,
              meta={"label": "Recompile registry.json from bindings — fixes a stale/bad-binding registry"})
def registry_rebuild() -> dict[str, Any]:
    """Recompile the served registry from bindings (e.g. after a connector fix). Does not
    touch the venv; still needs a restart for a warm worker to pick up new code."""
    from .ops import rebuild_registry
    r = rebuild_registry()
    return _ok(action="registry-rebuild", **r) if r.get("ok") else _fail(
        r.get("error") or "rebuild failed", "registry-rebuild", **r)


@conn.handler("runtime/command/restart", isolated=True,
              meta={"label": "Restart the node service (drops stale warm workers) — detached, survives this call"})
def runtime_restart() -> dict[str, Any]:
    from .ops import restart
    r = restart()
    return _ok(action="node-restart", **r) if r.get("ok") else _fail(
        r.get("error") or "restart failed", "node-restart", **r)


@conn.handler("worker/command/reload", isolated=True,
              meta={"label": "Reload workers (bare runtime: same as restart) — drop stale Python imports"})
def worker_reload() -> dict[str, Any]:
    from .ops import restart
    r = restart()
    return _ok(action="worker-reload", **r) if r.get("ok") else _fail(
        r.get("error") or "reload failed", "worker-reload", **r)


@conn.handler("runtime/command/upgrade", isolated=True,
              meta={"label": "Atomic upgrade: build a new release off to the side, smoke, switch, restart"})
def runtime_upgrade(release_id: str = "", spec: str = "urirun", connectors: list | None = None) -> dict[str, Any]:
    """Atomic upgrade via a fresh release dir — a failed build/smoke leaves the running
    node untouched (no half-updated node). ``release_id`` stamps the release; the caller
    passes a timestamp/sha (this handler must not read the clock)."""
    if not release_id:
        return _fail("release_id is required (a timestamp/sha; handlers can't read the clock)", "node-upgrade")
    from .ops import upgrade
    r = upgrade(release_id, spec=spec, connectors=connectors or [])
    return _ok(action="node-upgrade", **r) if r.get("ok") else _fail(
        r.get("error") or "upgrade failed", "node-upgrade", **r)


@conn.handler("runtime/command/rollback", isolated=True,
              meta={"label": "Roll back to the previous release (atomic symlink swap) + restart"})
def runtime_rollback() -> dict[str, Any]:
    from .ops import rollback
    r = rollback()
    return _ok(action="node-rollback", **r) if r.get("ok") else _fail(
        r.get("error") or "rollback failed", "node-rollback", **r)


@conn.handler("smoke/command/run", isolated=True,
              meta={"label": "Capability smoke: are the node's required routes/capabilities actually present?"})
def smoke_run(required_routes: list | None = None) -> dict[str, Any]:
    """Turns 'compatible' into 'ready': check the required routes are served. Reads the
    node's own /routes via the runtime state; a host passes the routes its task needs."""
    from .ops import runtime_state
    try:
        state = runtime_state()
        from urirun_fleet.smoke import required_routes_present
        # the node's own served routes are the source; caller supplies what it needs
        served = state.get("routes") or []
        rep = required_routes_present({"routes": served}, required_routes or [])
        return _ok(action="node-smoke", **rep, state=state)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), "node-smoke")


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    m = urirun.load_manifest(__package__) or {}
    try:
        from urirun_connectors_toolkit.connector_sdk import manifest_routes
        m["routes"] = manifest_routes(urirun_bindings())
    except Exception:  # noqa: BLE001
        pass
    return m


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=urirun.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())

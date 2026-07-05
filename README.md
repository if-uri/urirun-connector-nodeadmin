# urirun-connector-nodeadmin

The node-side management surface a [urirun-fleet](../urirun-fleet) reconcile plan drives.
A host computes drift and emits `node://` remedy URIs; this connector executes them ON the node.

| route | does |
|---|---|
| `node://{n}/runtime/query/state` | version/python/registry/lockfile — ground truth for diffing |
| `node://{n}/registry/command/rebuild` | recompile registry.json from bindings (fixes stale bindings) |
| `node://{n}/runtime/command/restart` | restart service (detached) — drop stale warm workers |
| `node://{n}/worker/command/reload` | reload workers (bare runtime = restart) |
| `node://{n}/runtime/command/upgrade` | ATOMIC: build release off to side → smoke → switch → restart |
| `node://{n}/runtime/command/rollback` | swap to previous release + restart |
| `node://{n}/smoke/command/run` | capability smoke: required routes present? |

Atomic releases reuse `urirun-fleet` `ReleaseManager`. These are ADMIN ops — the node must
gate `node://` behind an enrolled key / token.

Part of the ifURI solution · Author: Tom Sapletta · Apache-2.0

# Operations, recovery, and upgrades

OpenSCN 0.2 is a single-authority alpha for macOS and Linux. Cache tiers can share
immutable objects, but they do not replace the authoritative database or provide
metadata replication.

## Upgrading from 0.1

Stop the authoritative server and copy its entire data directory to a backup.
Install 0.2.0, then start the server with the same `--data` directory. New SQLite
tables are created automatically. Existing immutable objects, changeset IDs,
and refs are retained. Graph indexes for existing history are backfilled on
demand; the first query can therefore take longer than later queries. The HTTP
v1 interface remains compatible with existing clients.

Upgrade clients after completing any current command. Version 0.1 workspaces
need no manual metadata conversion. Do not run a 0.1 client against a workspace
with a 0.2 checkout journal: old clients do not understand recovery or locking.

## Interrupted checkouts

`update`, `merge`, and `rebase` fetch changed contents into a staging directory
inside `.openscn/`, then persist a checkout journal before changing files. If a
process stops during installation, mutating commands and status report that
recovery is required:

```sh
cd your-workspace
openscn recover
openscn status
```

Recovery uses verified staged contents and works without the server. It checks
pending operations against the old and intended file contents. If a file has a
conflicting local edit, recovery stops rather than overwriting it. Copy those
edits outside the workspace, restore the original file where the error asks,
and retry. Edits to already checkpointed files are left in place and appear as
local modifications after recovery. There is no force or rollback command.

Do not remove the journal or staging directory manually. If staged data is
missing or corrupted, preserve the current workspace and clone the target
changeset into a separate directory, then copy your local work across.

Workspace mutations take an exclusive lock and fail promptly if another command
holds it. Parallel agents should still use separate workspaces. Ordinary editors
do not honor this lock. Validation catches edits during network fetching and
rechecks each installation, but arbitrary writes at exactly the same time as
filesystem replacement cannot be made atomic. Pause editing during updates.

Tests inject process interruption at installation and checkpoint boundaries.
They do not establish complete hardware or power-loss guarantees. Staging needs
disk space for changed files. File payload memory is bounded by an individual
blob, while manifests and journal metadata remain in memory.

## Cache limits and failure isolation

```sh
openscn cache --upstream http://127.0.0.1:7777 --data .openscn-cache \
  --max-bytes 268435456 --max-upstream 16 --failure-threshold 3 \
  --cooldown 5 --max-workers 64
```

These defaults cap active upstream requests and HTTP handlers per process.
Saturation returns HTTP 503. Three consecutive upstream failures open a circuit
for five seconds; one probe is allowed afterward. Successful recovery closes
the circuit. Ordinary 4xx responses other than 429 do not count as an upstream
outage. Requests are not automatically retried, especially writes, because a
lost response can leave an operation's outcome unknown.

Already cached immutable objects remain readable while the circuit is open.
Cold reads and mutable metadata cannot bypass the configured upstream. Node and
regional cache tiers use identical endpoints; every tier applies its own bounds.
Repeated reads share an in-flight result even if disk caching is disabled.

Authenticated `GET /v1/cache/stats` returns cache lookup hits/misses, disk errors,
coalesced requests, active object fetches, upstream request/rejection counters,
and circuit/probe state. Lookup counts include internal rechecks; they are not
a measured fleet hit rate. Counters reset when the process restarts.

Authoritative servers also accept `--max-workers` (default 64). Neither limit is
a substitute for TLS termination, trusted-network access, or production quotas.

## Derived-data administration

The Python API allows local deterministic recipes; no endpoint accepts executable
recipes from clients. Custom recipes must be registered again after restart,
using the same name/version to reuse persisted results. Change the version when
changing the recipe's meaning.

```python
from openscn.store import Store

store = Store(".openscn-server")
store.register_derivation(
    "height", "1",
    lambda change, parents, key: 1 + max(parents, default=0),
    parents="all", eager=True,
)
height = store.derive("height", "main")
ancestor = store.first_parent_ancestor("main", 100)  # None beyond the root
```

`parents` may be `"all"`, `"first"`, or a deterministic function returning
prerequisite changeset IDs. Results and optional keys must be JSON serializable.
Eager recipes execute after a changeset is inserted; a recipe failure may leave
that immutable changeset stored, but does not move refs. Built-in `graph` and
`blame` recipes cannot be replaced.

With writers stopped, `store.clear_derived()` discards all derived caches and
indexes, leaving changesets, objects, and refs intact. Alternatively,
`store.rebuild_derived("graph", "main")` clears that recipe's values and rebuilds
its dependencies through main; other branches are backfilled when queried.
`store.rebuild_derived("blame", "main", "src/file.py")` rebuilds that file's blame.
Back up the entire store before administrative work. These helpers are not
online maintenance or backup/restore tools.

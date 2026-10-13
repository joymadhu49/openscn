# OpenSCN

Independent MIT-licensed implementation inspired by the public Scion article.
Python 3.11+, standard library runtime only. Do not claim Anthropic affiliation,
production readiness, or performance equivalence. Work only inside this repo.
Use unittest for correctness and integration tests. Never commit credentials or
local server data. Coordinate interface changes with the primary agent.

## Shared interface

`openscn.store.Store(directory)` owns SQLite metadata and compressed, immutable
SHA-256 objects. Public methods (JSON-serializable outputs):

- `put_blob(data: bytes) -> str`; `get_blob(oid) -> bytes`
- `create_changeset(parents: list[str], changes: dict[path, entry|null], message: str, author: str, timestamp: str|None=None) -> dict`
- `get_changeset(rev) -> dict` with `id, parents, changes, message, author, timestamp, root`
- `get_tree(oid) -> dict[name, entry]`, tree entries `{kind: 'tree', oid}` or `{kind: 'blob', oid, executable: bool}`
- `manifest(rev, prefixes: list[str]|None=None) -> dict[path, {oid, executable}]`
- `resolve(rev) -> str`; `refs() -> dict[name, oid]`
- `update_ref(name, target, expected: str|None) -> dict` (exact compare-and-swap, null expects absent)
- `log(rev, limit=50, path=None) -> list[dict]` (first-parent history)
- `diff(base, target) -> list[dict]` each `{path, before:entry|null, after:entry|null}`
- `merge_base(left, right) -> str|None`; `is_ancestor(ancestor, descendant) -> bool`
- `merge(left, right, message, author) -> dict` creates a two-parent changeset or raises conflict; file-level three-way merge
- `rebase(rev, onto, message, author) -> dict` replays single changeset or raises conflict
- `blame(rev, path) -> list[dict]` each `{line: int, text: str, changeset: str, author: str}`
- `first_parent_ancestor(rev, distance) -> str|None`
- `register_derivation(name, version, compute, *, parents='all', eager=False)`;
  `derive(name, rev, key=None)`; `clear_derived(name=None)`;
  `rebuild_derived(name, rev, key=None)`. See docs/operations.md for callback and
  administration semantics. Derived metadata is disposable; IDs/refs survive.

`openscn.errors.SCNError(message, status=400, details=None)` exposes those fields.

HTTP v1 (JSON except blob GET/PUT):
GET /health -> {status, version}; GET /v1/refs -> {refs}; GET /v1/changesets/{rev} -> changeset;
POST /v1/changesets -> create_changeset kwargs; GET /v1/trees/{oid} -> {entries};
PUT /v1/blobs (raw bytes) -> {oid}; GET /v1/blobs/{oid} -> raw bytes;
POST /v1/refs/{name} -> {target, expected} -> {name, target};
GET /v1/manifest/{rev}?prefix=a&prefix=b -> {files};
GET /v1/log/{rev}?limit=50&path=x -> {changesets};
GET /v1/diff?base=x&target=y -> {changes};
GET /v1/merge-base?left=x&right=y -> {base};
GET /v1/blame/{rev}?path=x -> {lines};
POST /v1/merge -> {left,right,message,author}; POST /v1/rebase -> {rev,onto,message,author}.
Cache only: authenticated GET /v1/cache/stats -> operational counters.
Errors: {error: message, details: optional}; HTTP status from SCNError.
Optional bearer auth via OPENSCN_TOKEN environment variable. Never persist token
in workspace config. Server listens on loopback by default.

CLI: `openscn serve --data DIR [--host 127.0.0.1] [--port 7777]`,
`openscn clone URL DIRECTORY [--rev main] [--sparse PATH ...]`,
`openscn status`, `openscn commit -m MSG [--author AUTHOR]`, `openscn log`,
`openscn diff [BASE TARGET]`, `openscn update [REV]`, `openscn land [--ref main]`,
`openscn blame PATH`, `openscn import-git PATH --server URL [--ref HEAD]`,
`openscn cache --upstream URL --data DIR [--port 7778]`.
Clone empty server creates an empty workspace; first commit has no parents.
Commit creates a changeset; land separately advances ref using CAS and ancestry check.
Workspace metadata lives in .openscn/; refuse symlink traversal and dirty updates.
Mutations use POSIX flock; interrupted staged checkouts require `openscn recover`.
Checkout retains one blob at a time; manifests remain in memory. Do not claim
full streaming or power-loss safety. HTTP handlers and upstream concurrency are
bounded; cache misses coalesce and fixed upstreams are never bypassed.

Ownership: core agent owns store.py/errors.py and core tests; client agent owns
client.py/cli.py and client tests; release agent owns docs, packaging, examples,
Docker and CI. Primary agent owns server.py/cache.py and integration tests.

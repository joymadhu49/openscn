# API reference

The HTTP API is rooted at `/v1`. Requests and responses are JSON unless noted.
Send `Content-Type: application/json` for JSON bodies and
`Authorization: Bearer <token>` when `OPENSCN_TOKEN` is configured. API author
fields are supplied by the caller. The server does not verify author identity.

Revisions may be full changeset IDs or reference names. URL-encode names and
query parameters. Object IDs are SHA-256 identifiers. Relative file paths use
`/`; absolute paths, traversal, and workspace metadata paths are rejected.
Object hashes include a type namespace: `SHA256(type_ascii + b"\0" + payload)`.
Trees use canonical JSON with sorted keys, compact separators, and UTF-8 text.
Request bodies are limited to 32 MiB. Requests with a browser `Origin` header
are rejected; this API is intended for the CLI and programmatic clients.
`/health` is accessible without a token.

## Data shapes

A changeset has `id`, `parents`, `changes`, `message`, `author`, `timestamp`, and
`root`. `root` identifies its complete immutable tree. In `changes`, a path maps
to `{ "oid": "blob-id", "executable": false }` or `null` to delete the path.
A tree maps a name to either `{ "kind": "tree", "oid": "tree-id" }` or
`{ "kind": "blob", "oid": "blob-id", "executable": false }`.

Errors use `{ "error": "message", "details": ... }`, with optional `details`.
Inspect the HTTP status and error body; unsuccessful writes must not be treated
as successful landings. A stale compare-and-swap or merge conflict returns a
conflict response.

## Endpoints

| Method and path | Request | Response |
| --- | --- | --- |
| `GET /health` | — | `{status, version}` |
| `PUT /v1/blobs` | Raw file bytes | `{oid}` |
| `GET /v1/blobs/{oid}` | — | Raw file bytes |
| `GET /v1/trees/{oid}` | — | `{entries}` |
| `POST /v1/changesets` | `{parents, changes, message, author, timestamp?}` | Changeset |
| `GET /v1/changesets/{rev}` | — | Changeset |
| `GET /v1/refs` | — | `{refs: {name: id}}` |
| `POST /v1/refs/{name}` | `{target, expected}` | `{name, target}` |
| `GET /v1/manifest/{rev}` | Optional repeated `prefix` query | `{files: {path: {oid, executable}}}` |
| `GET /v1/log/{rev}` | Optional `limit=50`, `path` query | `{changesets: [...]}` |
| `GET /v1/diff` | Required `base`, `target` query | `{changes: [{path, before, after}]}` |
| `GET /v1/merge-base` | Required `left`, `right` query | `{base: id or null}` |
| `POST /v1/merge` | `{left, right, message, author}` | Two-parent changeset |
| `POST /v1/rebase` | `{rev, onto, message, author}` | Replayed changeset |
| `GET /v1/blame/{rev}` | Required `path` query | `{lines: [{line, text, changeset, author}]}` |

The `expected` ref value is required: use `null` only when creating a name that
does not already exist. Otherwise use the exact previous changeset ID. Direct
ref updates enforce compare-and-swap and fast-forward ancestry.
The API does not automatically land newly created, merged, or rebased changesets.
Blob uploads can be reused by multiple changesets.

The cache uses `OPENSCN_TOKEN` for incoming authentication and, by default,
upstream authentication. Set `OPENSCN_UPSTREAM_TOKEN` separately if the upstream
uses a different token. Only already-cached immutable blob and tree reads can
succeed while the upstream is unavailable; metadata and checkout still need it.

## HTTP example

Start `openscn serve --data .openscn-server`, then use Python's standard library:

```python
import json
from urllib.request import Request, urlopen

def request(method, path, body, content_type="application/json"):
    req = Request("http://127.0.0.1:7777" + path, data=body, method=method,
                  headers={"Content-Type": content_type})
    with urlopen(req) as response:
        return json.load(response)

blob = request("PUT", "/v1/blobs", b"hello\n", "application/octet-stream")
change = request("POST", "/v1/changesets", json.dumps({
    "parents": [],
    "changes": {"hello.txt": {"oid": blob["oid"], "executable": False}},
    "message": "First change", "author": "Example",
}).encode())
request("POST", "/v1/refs/main", json.dumps({
    "target": change["id"], "expected": None,
}).encode())
```

This example expects an empty server without token authentication. For a more
complete isolated demonstration, run [`examples/demo.py`](../examples/demo.py).

## Embedded Python store

```python
from openscn.store import Store

store = Store(".openscn-server")
oid = store.put_blob(b"hello\n")
change = store.create_changeset(
    parents=[],
    changes={"hello.txt": {"oid": oid, "executable": False}},
    message="First change",
    author="Example",
)
store.update_ref("main", change["id"], expected=None)
assert store.get_blob(oid) == b"hello\n"
```

`Store(directory)` exposes the same operations locally:

| Method | Result |
| --- | --- |
| `put_blob(data: bytes)` | Blob ID |
| `get_blob(oid)` | Bytes |
| `create_changeset(parents, changes, message, author, timestamp=None)` | Changeset dict |
| `get_changeset(rev)` | Changeset dict |
| `get_tree(oid)` | Name-to-entry dict |
| `manifest(rev, prefixes=None)` | Path-to-file-entry dict |
| `resolve(rev)` | Changeset ID |
| `refs()` | Name-to-ID dict |
| `update_ref(name, target, expected)` | `{name, target}` |
| `log(rev, limit=50, path=None)` | First-parent changeset list |
| `diff(base, target)` | List of `{path, before, after}` |
| `merge_base(left, right)` | Changeset ID or `None` |
| `is_ancestor(ancestor, descendant)` | Boolean |
| `merge(left, right, message, author)` | Two-parent changeset or conflict |
| `rebase(rev, onto, message, author)` | Replayed changeset or conflict |
| `blame(rev, path)` | List of `{line, text, changeset, author}` |

Failures raise `openscn.errors.SCNError` with `message`, `status`, and `details`.
Only blobs are returned as bytes; other public data structures are JSON
serializable. Treat the version 0.x API as evolving and pin a release.

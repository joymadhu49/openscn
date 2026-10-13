# OpenSCN 0.1.0 — experimental first release

OpenSCN is an independent, MIT-licensed exploration of server-first source
control, inspired by the public Scion article. It is not affiliated with
Anthropic and is not a production-equivalent implementation of Scion.

This release includes:

- Immutable changesets and recursive trees backed by compressed SHA-256 objects
  and SQLite metadata.
- Sparse workspaces with bounded local caches and optional shared HTTP caches.
- Separate commit and land operations, with compare-and-swap and fast-forward
  protection for shared references.
- History, diffs, blame, file-level three-way merge, single-change rebase, and
  import of reachable history from a local Git repository.
- An HTTP API, optional bearer token authentication, a Docker recipe, tests,
  and an executable API demonstration.

## Install and try

Requires Python 3.11 or newer; there are no third-party runtime dependencies.

```sh
python -m pip install /path/to/openscn-0.1.0-py3-none-any.whl
openscn serve --data .openscn-server
```

In another terminal:

```sh
openscn clone http://127.0.0.1:7777 demo
cd demo
printf 'hello\n' > hello.txt
openscn commit -m 'First change' --author 'Your Name'
openscn land
```

The attached `SHA256SUMS` file records hashes for both distribution files.
The [README](https://github.com/joymadhu49/openscn#readme) provides the full
quickstart, cache setup, Docker instructions, and API links.

## Boundaries

This is an alpha release for local evaluation and development. It uses one
authoritative server with SQLite and disk storage. It does not include a
virtual filesystem, distributed metadata, fine-grained identity and access
management, or demonstrated enterprise-scale performance. Merge conflicts are
detected at file granularity. Use a trusted network and TLS proxy for network
deployments, and back up authoritative metadata and objects together.

Releases are distributed through GitHub; this workflow does not publish to
PyPI or operate a hosted service.

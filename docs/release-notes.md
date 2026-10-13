# OpenSCN 0.2.0 — indexed history and recoverable workspaces

This release brings the implementation closer to the public Scion design with
persisted derived data, efficient immutable-tree queries, and cache failure
isolation. OpenSCN remains an independent MIT-licensed alpha, with no Anthropic
affiliation or production-scale equivalence claim.

## Changes

- Persistent versioned derived-data recipes, eager graph indexes and
  first-parent skip pointers, indexed path history, and lazy cached blame.
- Merkle-aware diffs that avoid walking unchanged subtrees.
- Checkout staging with memory bounded by one blob, workspace mutation locks,
  and offline `openscn recover` for interrupted installation.
- Fixes for tracked and untracked files changed during checkout fetching.
- Coalesced cache misses, disposable cache disk failure handling, bounded
  upstream requests, an outage circuit breaker, and cache statistics.
- Independent fault review, a reproducible local benchmark, and a detailed
  [Scion design comparison](https://github.com/joymadhu49/openscn/blob/v0.2.0/docs/scion-comparison.md).

## Install

Requires Python 3.11+ on macOS or Linux. In an activated virtual environment:

```sh
python -m pip install /path/to/openscn-0.2.0-py3-none-any.whl
openscn serve --data .openscn-server
```

In another terminal using the same environment:

```sh
openscn clone http://127.0.0.1:7777 demo
cd demo
printf 'hello\n' > hello.txt
openscn commit -m 'First change' --author 'Your Name'
openscn land
```

The attached `SHA256SUMS` covers both distribution files. The
[README](https://github.com/joymadhu49/openscn#readme) includes sparse checkout,
cache setup, Git import, Docker, and API instructions.

## Upgrade and boundaries

Stop the server and back up its entire data directory before upgrading from
0.1. New derived tables are created automatically and old graph data backfills
on demand. Changeset IDs, refs, and HTTP v1 are preserved. See
[operations and recovery](https://github.com/joymadhu49/openscn/blob/v0.2.0/docs/operations.md).

One authoritative server still uses SQLite and local object storage. There is
no distributed metadata backend, virtual filesystem, per-user/path IAM, or
demonstrated production-scale performance. Merges remain file-level. Checkout
recovery is tested for process interruption, not all power-loss scenarios.
This release distributes software through GitHub and does not launch a hosted
source-control service.

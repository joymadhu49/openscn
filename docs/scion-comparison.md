# OpenSCN and Scion

OpenSCN is an independent MIT-licensed implementation inspired by a public
architecture description. It has no Anthropic affiliation, shared source code,
or claim of compatible protocols or equivalent performance.

## Design reference

Anthropic describes Scion as a service for clients that are usually online.
Changesets contain deltas and parents; moving a reference is a separate
compare-and-swap operation. Reusable derived structures answer repository
queries. Clients request immutable trees and blobs through cache tiers and
can select only relevant paths. The article also describes distributed
storage, on-demand workspaces, and directory permissions. These are design
ideas to compare against, not a public implementation specification.
[Source: Anthropic, “Redesigning source control at Anthropic,” October 13, 2026](https://www.anthropic.com/engineering/redesigning-source-control).

## Implementation and remaining gaps

The table describes OpenSCN's implementation. “Implemented” means code and
focused tests exist; it does not establish reliability at fleet scale.

| Area | OpenSCN implementation | Remaining boundary |
| --- | --- | --- |
| Online client and service | The HTTP API creates changesets and answers history, diff, blame, merge, and ancestry queries. Workspaces do not copy repository history. | Metadata operations need the authoritative service. There is no offline commit queue. |
| Changesets and refs | Immutable SHA-256 identities cover parents, file deltas, and metadata. Tree roots are derived separately. Ref updates use exact expected-value CAS; HTTP landing additionally requires fast-forward ancestry. | SQLite serializes metadata writes. Low-level `Store.update_ref` supplies CAS only. No distributed transaction layer or lock-free write claim. |
| Immutable object reads | Clients fetch hash-verified trees and blobs individually. Sparse traversal avoids downloading trees outside selected paths. | JSON trees and byte responses are buffered per object. Large flat directories still produce large tree objects. |
| Incremental tree writes | Creating a changeset rewrites directories along changed paths and reuses unchanged subtrees. | Each affected directory is encoded in full; work depends on directory widths as well as changed paths. |
| Derived data | A versioned recipe registry computes JSON values from prerequisite changesets and persists them in SQLite. Graph data is eager for new changesets; blame is lazy. Derived caches can be cleared and rebuilt without changing immutable changesets. | Recipes execute in the server process. There is no distributed work queue, distributed computation deduplication, or storage quota for derived results. Administrative rebuilds require stopped writers. |
| Graph and path queries | Generation metadata and first-parent jump pointers accelerate lineage queries. An indexed path-touch table finds candidate changes; branch filtering preserves first-parent log semantics, including directory deletion. | General DAG ancestry and merge-base still traverse graph nodes. Path history may inspect many candidates from unrelated branches. Old repositories require an initial graph backfill. |
| Blame and diff | Blame persists attribution per revision and path, depending on relevant first-parent touches. Diff skips equal subtree hashes rather than expanding both complete manifests. | Cold blame reads relevant history and stores complete line attribution for each computed revision. Line matching can be expensive on repetitive text. Merge and rebase still expand manifests. |
| Sparse checkout and recovery | Checkout stages one verified blob at a time on disk. A POSIX workspace lock serializes OpenSCN writers. A durable journal and progress checkpoints allow `openscn recover` to finish interrupted operations offline, preserving conflicting local work. | Memory includes the selected file manifest and largest fetched blob. Staging needs disk space for changed files. Files are eagerly materialized; there is no virtual filesystem. Locks do not stop editors, and recovery tests simulate process interruption rather than hardware power loss. |
| Shared caches | The HTTP cache has a bounded compressed disk store, verifies hashes, and coalesces simultaneous reads of the same missing object. Local cache disk failures fall back to the configured upstream. | Coalescing and eviction locks are process-local. There is no node daemon, Unix-socket protocol, memory streaming tier, or cache snapshot distribution. |
| Cache outage protection | Fixed upstream routing, bounded upstream requests, a circuit breaker, and bounded HTTP handlers limit load. Authenticated cache statistics expose hits, misses, coalesced requests, and upstream activity. | Limits apply to each process. No fleet-wide quotas, topology controller, or automatic scaling. Cold reads fail when the configured upstream is unavailable. |
| Merge and rebase | The server performs file-level three-way merges and replays one changeset onto another. | Different edits to the same file conflict, even when line ranges do not overlap. No rename detection, interactive conflict resolver, or automatic stack rebase. |
| Git import | Import preserves reachable parent relationships, file contents, executable bits, and deletions before landing the imported tip. | Import is one-way. No Git wire protocol, export, incremental synchronization service, symlink import, or submodules. |
| Authorization | Optional shared bearer-token authentication and loopback defaults protect the prototype API. | No per-user identities, path permissions, audit service, tenant isolation, or built-in TLS. Author fields are caller-supplied metadata. |
| Storage and operations | One authoritative server uses SQLite WAL metadata and compressed local immutable objects. Optional cache servers use the same object endpoints. | No replicated metadata backend, sharded object service, tested disaster recovery tooling, garbage collection, code-review application, or integrated CI service. |

## Independent regression review

`tests/test_review.py` exercises faults across component boundaries: local cache
read/write errors, simultaneous cold reads, zero-budget caches, shared upstream
failures, corrupt upstream content, concurrent edits during checkout fetches,
and competing ref writers. The tests use isolated local servers and injected
faults. They also check directory deletion and re-addition through indexed
history, first-parent blame across branches, rebuilding derived data, and
preserving edits to already completed operations during offline recovery.
They establish those specific behaviors, not production throughput,
power-loss durability, or safety against arbitrary concurrent filesystem edits.

Run the review checks from the repository with Python 3.11 or newer:

```sh
python -m unittest discover -s tests -p test_review.py -v
```

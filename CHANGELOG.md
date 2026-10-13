# Changelog

## 0.2.0 — 2026-10-14

- Add versioned persistent derived-data recipes, eager graph indexes,
  first-parent skip pointers, indexed path history, and lazy cached blame.
- Compare Merkle subtrees directly for diffs, skipping unchanged directories.
- Stage checkout contents on disk with memory bounded by individual blobs;
  serialize workspace mutations and resume interrupted updates with `recover`.
- Preserve tracked edits and untracked files created while checkout fetches.
- Coalesce concurrent cache misses, tolerate cache disk failures, bound HTTP
  and upstream concurrency, and stop repeated origin requests during outages.
- Add independent fault tests, migration/rebuild checks, a reproducible query
  benchmark, and an explicit comparison with the public Scion design.

Existing changeset IDs and the HTTP v1 interface are preserved. Old stores gain
new derived tables automatically, with graph backfill on demand. Stop writers
and back up the entire store before upgrading. This remains an experimental
single-node release; see the architecture comparison for remaining gaps.

## 0.1.0 — 2026-10-14

Initial experimental release:

- Immutable SHA-256 blobs, trees, and changesets backed by disk and SQLite.
- HTTP API, optimistic ref updates, history, diff, file-level merge and rebase,
  and blame.
- Sparse workspaces, local immutable-object caching, shared HTTP cache tiers,
  commit and land workflow, `.openscnignore`, and local Git history import.
- Optional shared bearer token, loopback defaults, and workspace path checks.
- Python packaging, Docker image recipe, automated tests, API demonstration,
  and GitHub release builds.

This release does not provide a virtual filesystem, distributed metadata,
fine-grained permissions, automated garbage collection, or proven production
scale. See the README for deployment boundaries.

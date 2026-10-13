# Changelog

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

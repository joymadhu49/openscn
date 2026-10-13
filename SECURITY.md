# Security

OpenSCN 0.1 is an experimental local-first deployment of a server-first source
control design. It has not undergone an independent security audit. The
authoritative server contains all uploaded source content and history.

Commit includes all changed and new selected files, without a separate staging
step. Configure `.openscnignore` before working with secrets or generated files
and inspect `openscn status` before committing. `.gitignore` is not read. Ignore
rules do not remove tracked files or erase previously uploaded content.

The default listen address is loopback. `OPENSCN_TOKEN` enables a shared bearer
token; without it, anyone who can reach the loopback server can use its API.
Non-loopback binds require a token. The health endpoint is unauthenticated.
Configure
the token on clients and intermediate caches through the environment. Do not
put credentials in URLs, repository files, issues, or shell examples shared
with others. Author names are metadata, not authenticated identities.

For network deployments, terminate TLS at a trusted reverse proxy and restrict
access to trusted users. The built-in HTTP server does not provide TLS,
fine-grained authorization, per-user quotas, or hardened public multi-tenancy.
Caches hold source content too and need the same filesystem and network access
restrictions. Only point a cache at an upstream you trust.

Workspace operations reject dangerous paths and symlink traversal. These checks
do not make an untrusted repository safe to execute: review imported scripts
and build commands before running them. Keep the authoritative data directory
outside checkouts and restrict its filesystem permissions. Back up metadata and
objects together while the server is stopped.

To report a vulnerability, use the repository's private GitHub security advisory
reporting option if it is available. If private reporting is unavailable, open
an issue requesting a private contact channel without including vulnerability
details, credentials, exploit code, or private source content. There is no
guaranteed response SLA for this volunteer project.

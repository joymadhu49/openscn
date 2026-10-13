# OpenSCN

**Server-first source control for agents and humans.** OpenSCN is a small,
independent implementation of immutable changesets, shared history, sparse
workspaces, and content-addressed caches. It is MIT licensed, runs on Python
3.11+, and has no runtime dependencies outside the Python standard library.

Inspired by the ideas in Anthropic's
[Redesigning source control](https://www.anthropic.com/engineering/redesigning-source-control).
OpenSCN is not Scion, is not affiliated with Anthropic, and makes no claim of
feature, performance, or operational equivalence. This release is an
experimental single-node implementation, suitable for learning and evaluation.
See the [design comparison and review](docs/scion-comparison.md) for implemented
principles, tested behavior, and remaining gaps.

## Install

Install a pinned release directly from GitHub:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'git+https://github.com/joymadhu49/openscn.git@v0.2.0'
openscn --help
```

Alternatively, download the wheel from
[GitHub Releases](https://github.com/joymadhu49/openscn/releases) and run
`python -m pip install /path/to/openscn-0.2.0-py3-none-any.whl`.
There is no requirement to install from PyPI. This release targets macOS and
Linux (POSIX filesystems); Windows is not tested or supported yet.

## Try it in two terminals

Start the authoritative server in terminal one:

```sh
openscn serve --data .openscn-server --port 7777
```

In terminal two, with the same virtual environment active:

```sh
openscn clone http://127.0.0.1:7777 demo
cd demo
printf 'Hello, OpenSCN!\n' > hello.txt
openscn status
openscn commit -m 'Add a greeting' --author 'Your Name'
openscn land
openscn log
openscn blame hello.txt
```

`commit` uploads changed content and creates an immutable changeset. `land`
separately advances the shared `main` reference, using compare-and-swap and an
ancestry check to protect against concurrent writers. Workspace history stays
on the server; cloning does not copy every past version to your computer.

`commit` includes all changed and new files in the workspace's selection; there
is no staging area. Before adding an existing project, create `.openscnignore`
to exclude local secrets, environments, and generated files, then inspect
`openscn status`:

```text
# .openscnignore
.env
.env.*
.venv/
node_modules/
__pycache__/
dist/
```

Ignore rules apply to untracked files; tracked changes are always included.
Patterns without `/` match names at any depth, patterns containing `/` match
workspace-relative paths, and trailing `/` selects a directory and its
contents. Shell-style wildcards are supported. Blank lines and `#` comments
are ignored; `!` negation is unsupported. `.gitignore` is not read, and only
`.git` and `.openscn` are excluded automatically.

For an automated API demonstration, run `python examples/demo.py` from a checkout
after installing the package. It starts a temporary local server, creates
changesets, performs a merge, checks sparse reads and a rejected stale ref
update, then removes its temporary data.

## Sparse workspaces and caches

```sh
openscn clone http://127.0.0.1:7777 backend --sparse src/backend docs
cd backend
openscn update main
```

A sparse workspace materializes only the selected paths. Changes outside that
selection remain in its changeset tree. The client walks selected tree branches
and downloads the needed blobs; its bounded disk cache stores immutable objects.

An optional shared cache sits between clients and the authoritative server:

```sh
openscn cache --upstream http://127.0.0.1:7777 --data .openscn-cache --port 7778
openscn clone http://127.0.0.1:7778 cached-demo
```

The cache stores immutable blob and tree responses and forwards metadata and
writes to the primary server. You can chain these caches. Mutable references
must still be read from the primary; a cache is not an independent replica.

Simultaneous requests for a missing object share one upstream fetch. Each cache
defaults to at most 16 active upstream requests, and temporarily stops new
upstream requests after three consecutive failures. Cached immutable reads
continue during an outage. Configure this with `--max-upstream`,
`--failure-threshold`, and `--cooldown`; `/v1/cache/stats` exposes counters using
the same authentication as other API endpoints. Each tier uses only its
configured upstream and never bypasses it. Cache disk failures do not prevent
healthy upstream reads.

Checkouts stage one verified blob at a time before changing workspace files,
then journal their progress. If interrupted, run `openscn recover` in that
workspace to finish from the staged files, even while offline. Recovery refuses
to overwrite conflicting edits; preserve those files elsewhere before retrying.
Do not delete `.openscn/` or its staging data while recovery is pending. See
[workspace recovery](docs/operations.md) for details and limits.

## Branching, merging, and Git import

Changesets form a DAG. A workspace can create changesets without first creating
a named branch. To merge another changeset into a clean workspace:

```sh
openscn merge CHANGESET_ID -m 'Merge parallel work'
openscn land
```

`openscn rebase ONTO -m 'Replay current change'` replays the current changeset
onto another revision. Merges are **file-level three-way merges**: independent
file edits merge automatically, but different edits to the same file conflict,
even when their lines do not overlap. Conflict responses identify affected
paths. There is no interactive conflict editor.

Import the history reachable from a local Git revision, with Git installed:

```sh
openscn import-git /path/to/git-repository --server http://127.0.0.1:7777 --ref HEAD --target main
```

Import targets a regular local Git repository. Git-specific objects such as
submodules and symbolic links are not OpenSCN workspace features. Inspect import
errors before relying on the result; OpenSCN is not a Git wire-protocol server.

## Operating the prototype

The server binds to `127.0.0.1` by default. Set `OPENSCN_TOKEN` in the environment
of the server, clients, and any cache to require bearer authentication. Tokens
are not stored in workspace configuration. Use a TLS-terminating reverse proxy
and restrict network access before exposing a server outside your machine.
See [SECURITY.md](SECURITY.md).

```sh
docker build -t openscn .
export OPENSCN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker run --rm -p 127.0.0.1:7777:7777 -e OPENSCN_TOKEN -v openscn-data:/data openscn
```

Use that same token in the client environment. Binding to a non-loopback
address, including inside a container, requires a token. The Docker command is
for local evaluation. Back up the **entire server data
directory** while the server is stopped; it contains both metadata and objects.
Losing either can make history unreadable. There is no automatic object garbage
collection, hosted service, replicated metadata database, fine-grained access
control, virtual filesystem, streaming checkout, or demonstrated large-scale
performance in this release. Client and proxy caches can be discarded; the
authoritative store cannot. Authentication is one shared token for all users.
Mutating commands take an exclusive workspace lock and reject overlapping
commands; parallel agents should use separate workspaces. Checkout recovery is
tested for process interruption, but is not a blanket power-loss guarantee or a
replacement for backups. Arbitrary concurrent writes from external editors
cannot be made atomic with checkout; pause editing while updating.

## Develop

```sh
git clone https://github.com/joymadhu49/openscn.git
cd openscn
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/demo.py
python examples/benchmark.py --files 1000 --commits 200
python -m pip install build
python -m build
```

See the [architecture](docs/architecture.md), [HTTP and Python API](docs/api.md),
[operations and recovery](docs/operations.md), [local measurements](docs/performance.md),
[contribution guide](CONTRIBUTING.md), and [changelog](CHANGELOG.md).
Code is available under the [MIT license](LICENSE).

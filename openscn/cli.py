"""Command-line interface for OpenSCN."""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import sys
import urllib.parse

from . import __version__
from .client import DEFAULT_CACHE_BYTES, Workspace, import_git
from .errors import SCNError


def _author():
    return getpass.getuser()


def parser():
    root = argparse.ArgumentParser(prog="openscn", description="Server-first source control with sparse working copies")
    root.add_argument("--version", action="version", version="OpenSCN " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the authoritative object and metadata server")
    serve.add_argument("--data", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7777)
    serve.add_argument("--max-workers", type=int, default=64)
    cache = commands.add_parser("cache", help="Run a bounded HTTP read-through cache")
    cache.add_argument("--upstream", required=True)
    cache.add_argument("--data", required=True)
    cache.add_argument("--host", default="127.0.0.1")
    cache.add_argument("--port", type=int, default=7778)
    cache.add_argument("--max-bytes", type=int, default=DEFAULT_CACHE_BYTES)
    cache.add_argument("--max-upstream", type=int, default=16)
    cache.add_argument("--failure-threshold", type=int, default=3)
    cache.add_argument("--cooldown", type=float, default=5.0)
    cache.add_argument("--max-workers", type=int, default=64)
    clone = commands.add_parser("clone", help="Create a working copy, optionally restricted to path prefixes")
    clone.add_argument("url")
    clone.add_argument("directory")
    clone.add_argument("--rev", default="main")
    clone.add_argument("--sparse", nargs="+", metavar="PATH")
    clone.add_argument("--cache-bytes", type=int, default=DEFAULT_CACHE_BYTES)
    commands.add_parser("status", help="Show local additions, modifications, and deletions")
    commands.add_parser("recover", help="Safely continue an interrupted checkout from local staging")
    commit = commands.add_parser("commit", help="Upload local changes as a changeset; does not advance a ref")
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("--author", default=_author())
    update = commands.add_parser("update", help="Check out a revision while protecting local work")
    update.add_argument("rev", nargs="?")
    land = commands.add_parser("land", help="Compare-and-swap a ref to the current changeset")
    land.add_argument("--ref")
    log = commands.add_parser("log", help="Show first-parent history")
    log.add_argument("--limit", type=int, default=20)
    log.add_argument("--path")
    diff = commands.add_parser("diff", help="Show local changes, or differences between two revisions")
    diff.add_argument("revisions", nargs="*", metavar="REV")
    blame = commands.add_parser("blame", help="Show line provenance")
    blame.add_argument("path")
    for operation, help_text, argument in (("merge", "Merge a revision into the working-copy changeset", "rev"), ("rebase", "Replay the current changeset onto a revision", "onto")):
        sub = commands.add_parser(operation, help=help_text)
        sub.add_argument(argument)
        sub.add_argument("-m", "--message", required=True)
        sub.add_argument("--author", default=_author())
    importer = commands.add_parser("import-git", help="Import a Git revision and all reachable ancestors")
    importer.add_argument("path")
    importer.add_argument("--server", required=True)
    importer.add_argument("--ref", default="HEAD", help="Git revision to import (default: HEAD)")
    importer.add_argument("--target", default="main", help="OpenSCN ref to land (default: main)")
    return root


def _print_diff(workspace, changes, local=False):
    for change in changes:
        path, before, after = change["path"], change["before"], change["after"]
        old = workspace.client.get_blob(before["oid"]) if before else b""
        new = workspace._safe(path).read_bytes() if local and after else workspace.client.get_blob(after["oid"]) if after else b""
        print(f"diff --openscn a/{path} b/{path}")
        if before is None:
            print("new file mode " + ("100755" if after["executable"] else "100644"))
        elif after is None:
            print("deleted file mode " + ("100755" if before["executable"] else "100644"))
        elif before["executable"] != after["executable"]:
            print("old mode " + ("100755" if before["executable"] else "100644"))
            print("new mode " + ("100755" if after["executable"] else "100644"))
        try:
            if b"\0" in old or b"\0" in new:
                raise UnicodeError
            old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeError:
            print("Binary files differ")
            continue
        for line in difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile="a/" + path if before else "/dev/null", tofile="b/" + path if after else "/dev/null"):
            print(line, end="" if line.endswith("\n") else "\n\\ No newline at end of file\n")


def run(args):
    if args.command == "serve":
        from .server import serve
        serve(args.data, args.host, args.port, args.max_workers)
        return
    if args.command == "cache":
        from .cache import serve
        serve(args.upstream, args.data, args.host, args.port, args.max_bytes, args.max_upstream, args.failure_threshold, args.cooldown, args.max_workers)
        return
    if args.command == "clone":
        workspace = Workspace.clone(args.url, args.directory, args.rev, args.sparse, args.cache_bytes)
        print(f"Cloned {len(workspace.metadata['files'])} files into {workspace.root}")
        return
    if args.command == "import-git":
        result = import_git(args.path, args.server, args.ref, args.target)
        print(f"Imported {result['commits']} changesets; {result['ref']} -> {result['head']}")
        return
    workspace = Workspace.open()
    if args.command == "status":
        print("HEAD " + (workspace.metadata["head"] or "(empty)"))
        changes = workspace.status()
        if not changes:
            print("Working copy clean")
        for change in changes:
            print(f"{ {'added': 'A', 'modified': 'M', 'deleted': 'D'}[change['status']]} {change['path']}")
    elif args.command == "commit":
        result = workspace.commit(args.message, args.author)
        print(f"Created {result['id']}\nRun openscn land to advance {workspace.metadata['ref']}.")
    elif args.command == "recover":
        print("Recovered checkout to " + workspace.recover())
    elif args.command == "update":
        print("Updated to " + workspace.update(args.rev)["id"])
    elif args.command == "land":
        result = workspace.land(args.ref)
        print(f"Landed {result['target']} on {result['name']}")
    elif args.command in ("merge", "rebase"):
        revision = args.rev if args.command == "merge" else args.onto
        result = workspace.integrate(args.command, revision, args.message, args.author)
        print(f"Created {result['id']}\nRun openscn land to advance {workspace.metadata['ref']}.")
    elif args.command == "diff":
        if not args.revisions:
            _print_diff(workspace, workspace.status(), local=True)
        elif len(args.revisions) == 2:
            query = urllib.parse.urlencode({"base": args.revisions[0], "target": args.revisions[1]})
            _print_diff(workspace, workspace.client.request("GET", "/v1/diff?" + query)["changes"])
        else:
            raise SCNError("diff requires either no revisions or BASE TARGET")
    elif args.command == "log":
        if workspace.metadata["head"] is None:
            print("No changesets")
            return
        query = {"limit": args.limit}
        if args.path:
            query["path"] = args.path
        changesets = workspace.client.request("GET", "/v1/log/" + workspace.metadata["head"] + "?" + urllib.parse.urlencode(query))["changesets"]
        for changeset in changesets:
            print(f"{changeset['id']}\nAuthor: {changeset['author']}\nDate:   {changeset['timestamp']}\n\n    {changeset['message']}\n")
    elif args.command == "blame":
        if workspace.metadata["head"] is None:
            raise SCNError("No changesets")
        result = workspace.client.request("GET", "/v1/blame/" + workspace.metadata["head"] + "?" + urllib.parse.urlencode({"path": args.path}))
        for line in result["lines"]:
            print(f"{line['changeset'][:12]} ({line['author']}) {line['line']:4} {line['text'].rstrip(chr(10))}")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(args)
    except SCNError as exc:
        print(f"openscn: {exc}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError) as exc:
        print(f"openscn: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None

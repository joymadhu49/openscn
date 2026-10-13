"""A small centralized version store with immutable objects and mutable CAS refs.

Changeset identity includes its delta and parents, but not the derived tree root.
Tree updates rewrite only directories along changed paths. Each operation owns
its SQLite connection, so a Store can be shared by HTTP handler threads.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zlib

from .errors import SCNError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validate_path(path):
    if (not isinstance(path, str) or not path or len(path) > 4096
            or len(path.split("/")) > 128
            or "\\" in path or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in path)
            or any(c.casefold() in ("", ".", "..", ".git", ".openscn") for c in path.split("/"))):
        raise SCNError("Invalid repository path")
    return path


def validate_oid(oid):
    if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{64}", oid):
        raise SCNError("Invalid object identifier")
    return oid


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.objects = self.directory / "objects"
        if self.objects.is_symlink() or (self.directory / "metadata.sqlite3").is_symlink():
            raise SCNError("Store paths cannot be symbolic links")
        self.objects.mkdir(exist_ok=True)
        self.database = self.directory / "metadata.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS changesets (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refs (
                    name TEXT PRIMARY KEY, target TEXT NOT NULL REFERENCES changesets(id)
                );
            """)
        self.empty_tree = self._put_object("tree", canonical({}))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _object_path(self, oid):
        validate_oid(oid)
        directory = self.objects / oid[:2]
        path = directory / oid[2:]
        if directory.is_symlink() or path.is_symlink():
            raise SCNError("Object paths cannot be symbolic links")
        return path

    def _put_object(self, kind, payload):
        data = kind.encode("ascii") + b"\0" + payload
        oid = hashlib.sha256(data).hexdigest()
        path = self._object_path(oid)
        if path.exists():
            self._get_object(oid, kind)
            return oid
        if not path.parent.exists():
            path.parent.mkdir(exist_ok=True)
            self._sync_directory(self.objects)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(zlib.compress(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return oid

    @staticmethod
    def _sync_directory(directory):
        # Windows does not expose directory fsync through os.open.
        if os.name != "nt":
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _get_object(self, oid, kind):
        path = self._object_path(oid)
        try:
            data = zlib.decompress(path.read_bytes())
        except FileNotFoundError:
            raise SCNError("Object not found", 404) from None
        except zlib.error:
            raise SCNError("Corrupt object", 500) from None
        if hashlib.sha256(data).hexdigest() != oid:
            raise SCNError("Object checksum mismatch", 500)
        prefix = kind.encode("ascii") + b"\0"
        if not data.startswith(prefix):
            raise SCNError("Object has the wrong type")
        return data[len(prefix):]

    def put_blob(self, data):
        if not isinstance(data, bytes):
            raise SCNError("Blob must be bytes")
        return self._put_object("blob", data)

    def get_blob(self, oid):
        return self._get_object(oid, "blob")

    def get_tree(self, oid):
        return json.loads(self._get_object(oid, "tree"))

    def resolve(self, rev):
        if not isinstance(rev, str) or not rev:
            raise SCNError("Revision must be a nonempty string")
        with self._connect() as db:
            row = db.execute("SELECT target FROM refs WHERE name=?", (rev,)).fetchone()
            if row:
                return row[0]
            row = db.execute("SELECT id FROM changesets WHERE id=?", (rev,)).fetchone()
            if row:
                return row[0]
        raise SCNError("Revision not found: " + rev, 404)

    def get_changeset(self, rev):
        oid = self.resolve(rev)
        with self._connect() as db:
            payload, root = db.execute("SELECT payload, root FROM changesets WHERE id=?", (oid,)).fetchone()
        return dict(json.loads(payload), id=oid, root=root)

    def _entry(self, entry):
        if not isinstance(entry, dict) or set(entry) - {"oid", "executable"} or "oid" not in entry:
            raise SCNError("File entry must contain oid and optional executable")
        executable = entry.get("executable", False)
        if not isinstance(executable, bool):
            raise SCNError("Executable must be a boolean")
        self.get_blob(entry["oid"])
        return {"oid": entry["oid"], "executable": executable}

    def _edit_tree(self, root, parts, entry):
        tree = self.get_tree(root)
        name = parts[0]
        if len(parts) == 1:
            if entry is None:
                tree.pop(name, None)
            else:
                if name in tree and tree[name]["kind"] == "tree":
                    raise SCNError("Delete the existing directory before replacing it with a file")
                tree[name] = dict(entry, kind="blob")
        else:
            previous = tree.get(name)
            if entry is None and (previous is None or previous["kind"] != "tree"):
                return root
            if previous and previous["kind"] == "blob":
                raise SCNError("Delete the existing file before replacing it with a directory")
            child = previous["oid"] if previous and previous["kind"] == "tree" else self.empty_tree
            result = self._edit_tree(child, parts[1:], entry)
            if result == self.empty_tree:
                tree.pop(name, None)
            else:
                tree[name] = {"kind": "tree", "oid": result}
        return self._put_object("tree", canonical(tree))

    def create_changeset(self, parents, changes, message, author, timestamp=None):
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            raise SCNError("Parents must be a list of revisions")
        parents = [self.resolve(p) for p in parents]
        if len(parents) != len(set(parents)):
            raise SCNError("Duplicate parents")
        if not isinstance(changes, dict):
            raise SCNError("Changes must be a mapping")
        if not isinstance(message, str) or not isinstance(author, str):
            raise SCNError("Message and author must be strings")
        if timestamp is not None and not isinstance(timestamp, str):
            raise SCNError("Timestamp must be a string")
        normalized = {validate_path(path): None if entry is None else self._entry(entry)
                      for path, entry in changes.items()}
        live = {path for path, entry in normalized.items() if entry is not None}
        for path in live:
            parts = path.split("/")
            if any("/".join(parts[:i]) in live for i in range(1, len(parts))):
                raise SCNError("A path cannot be both a file and a directory")
        root = self.get_changeset(parents[0])["root"] if parents else self.empty_tree
        # Remove first so directory/file replacements are independent of input order.
        for path, entry in sorted(normalized.items(), key=lambda item: (item[1] is not None, item[0])):
            root = self._edit_tree(root, path.split("/"), entry)
        payload = {"parents": parents, "changes": normalized, "message": message,
                   "author": author, "timestamp": timestamp or datetime.now(timezone.utc).isoformat()}
        encoded = canonical(payload)
        oid = hashlib.sha256(b"changeset\0" + encoded).hexdigest()
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO changesets(id,payload,root) VALUES (?,?,?)",
                       (oid, encoded.decode("utf-8"), root))
        return dict(payload, id=oid, root=root)

    def manifest(self, rev, prefixes=None):
        if prefixes is not None and (not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes)):
            raise SCNError("Prefixes must be a list of paths")
        prefixes = [validate_path(p) for p in prefixes] if prefixes else []
        result = {}
        def visit(root, parent=""):
            for name, entry in self.get_tree(root).items():
                path = parent + name
                if prefixes and not any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in prefixes):
                    continue
                if entry["kind"] == "tree":
                    visit(entry["oid"], path + "/")
                elif not prefixes or any(path == p or path.startswith(p + "/") for p in prefixes):
                    result[path] = {"oid": entry["oid"], "executable": entry["executable"]}
        visit(self.get_changeset(rev)["root"])
        return result

    def refs(self):
        with self._connect() as db:
            return dict(db.execute("SELECT name,target FROM refs ORDER BY name"))

    def update_ref(self, name, target, expected):
        validate_path(name)
        if re.fullmatch(r"[0-9a-f]{64}", name):
            raise SCNError("Reference name cannot be an object identifier")
        target = self.resolve(target)
        if expected is not None:
            validate_oid(expected)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT target FROM refs WHERE name=?", (name,)).fetchone()
            actual = row[0] if row else None
            if actual != expected:
                raise SCNError("Reference changed; refresh and retry", 409,
                               {"name": name, "expected": expected, "actual": actual})
            db.execute("INSERT INTO refs(name,target) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target", (name, target))
        return {"name": name, "target": target}

    def log(self, rev, limit=50, path=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0 or limit > 10000:
            raise SCNError("Limit must be between 0 and 10000")
        if path is not None:
            validate_path(path)
        result = []
        current = self.resolve(rev)
        while current and len(result) < limit:
            change = self.get_changeset(current)
            if path is None or any(p == path or p.startswith(path + "/") or path.startswith(p + "/") for p in change["changes"]):
                result.append(change)
            current = change["parents"][0] if change["parents"] else None
        return result

    @staticmethod
    def _diff_manifests(before, after):
        return [{"path": p, "before": before.get(p), "after": after.get(p)}
                for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]

    def diff(self, base, target):
        return self._diff_manifests(self.manifest(base), self.manifest(target))

    def _ancestors(self, rev):
        distances = {}
        queue = deque([(self.resolve(rev), 0)])
        while queue:
            current, distance = queue.popleft()
            if current in distances:
                continue
            distances[current] = distance
            queue.extend((p, distance + 1) for p in self.get_changeset(current)["parents"])
        return distances

    def is_ancestor(self, ancestor, descendant):
        return self.resolve(ancestor) in self._ancestors(descendant)

    def merge_base(self, left, right):
        a, b = self._ancestors(left), self._ancestors(right)
        common = a.keys() & b.keys()
        if not common:
            return None
        # Discard common ancestors superseded by another common ancestor.
        best = set(common)
        for oid in sorted(common, key=lambda p: (a[p] + b[p], p)):
            if oid in best:
                best.difference_update(self._ancestors(oid).keys() - {oid})
        return min(best, key=lambda p: (a[p] + b[p], max(a[p], b[p]), p))

    @staticmethod
    def _merge_files(base, left, right):
        merged, conflicts = {}, []
        for path in sorted(base.keys() | left.keys() | right.keys()):
            old, ours, theirs = base.get(path), left.get(path), right.get(path)
            if ours == theirs or theirs == old:
                value = ours
            elif ours == old:
                value = theirs
            else:
                conflicts.append(path)
                continue
            if value is not None:
                merged[path] = value
        for path in merged:
            parts = path.split("/")
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                if ancestor in merged:
                    conflicts.extend([ancestor, path])
        if conflicts:
            raise SCNError("Merge conflicts require resolution", 409, {"conflicts": sorted(set(conflicts))})
        return merged

    def merge(self, left, right, message, author):
        left, right = self.resolve(left), self.resolve(right)
        if left == right:
            raise SCNError("Cannot merge a changeset with itself")
        base = self.merge_base(left, right)
        ours = self.manifest(left)
        merged = self._merge_files(self.manifest(base) if base else {}, ours, self.manifest(right))
        delta = {d["path"]: d["after"] for d in self._diff_manifests(ours, merged)}
        return self.create_changeset([left, right], delta, message, author)

    def rebase(self, rev, onto, message, author):
        original = self.get_changeset(rev)
        onto = self.resolve(onto)
        base = self.manifest(original["parents"][0]) if original["parents"] else {}
        ours = self.manifest(onto)
        merged = self._merge_files(base, ours, self.manifest(original["id"]))
        delta = {d["path"]: d["after"] for d in self._diff_manifests(ours, merged)}
        return self.create_changeset([onto], delta, message, author)

    def blame(self, rev, path):
        validate_path(path)
        current = self.get_changeset(rev)
        if path not in self.manifest(current["id"], [path]):
            raise SCNError("File not found", 404)
        history = []
        while current:
            history.append(current)
            current = self.get_changeset(current["parents"][0]) if current["parents"] else None
        old_lines, attribution = [], []
        for change in reversed(history):
            entry = self.manifest(change["id"], [path]).get(path)
            try:
                lines = self.get_blob(entry["oid"]).decode("utf-8").splitlines(keepends=True) if entry else []
            except UnicodeDecodeError:
                # A binary historical version breaks line continuity, but does
                # not stop blame when the requested version is UTF-8 text.
                if change["id"] == history[0]["id"]:
                    raise SCNError("Blame requires a UTF-8 text file") from None
                old_lines, attribution = [], []
                continue
            updated = [(change["id"], change["author"])] * len(lines)
            for block in SequenceMatcher(None, old_lines, lines, autojunk=False).get_matching_blocks():
                updated[block.b:block.b + block.size] = attribution[block.a:block.a + block.size]
            old_lines, attribution = lines, updated
        return [{"line": i + 1, "text": line, "changeset": attribution[i][0], "author": attribution[i][1]}
                for i, line in enumerate(old_lines)]

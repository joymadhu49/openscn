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
                CREATE TABLE IF NOT EXISTS derived_values (
                    name TEXT NOT NULL, version TEXT NOT NULL,
                    changeset TEXT NOT NULL REFERENCES changesets(id),
                    cache_key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(name, version, changeset, cache_key)
                );
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id TEXT PRIMARY KEY REFERENCES changesets(id),
                    generation INTEGER NOT NULL, first_depth INTEGER NOT NULL,
                    jumps TEXT NOT NULL, parents TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS path_changes (
                    path TEXT NOT NULL, changeset TEXT NOT NULL REFERENCES changesets(id),
                    PRIMARY KEY(path, changeset)
                );
                CREATE INDEX IF NOT EXISTS path_changes_changeset ON path_changes(changeset);
            """)
        self.empty_tree = self._put_object("tree", canonical({}))
        self._derivations = {}
        self.register_derivation("graph", "1", self._compute_graph, eager=True)
        self.register_derivation("blame", "1", self._compute_blame, parents=self._blame_parents)

    def register_derivation(self, name, version, compute, *, parents="all", eager=False):
        """Register a deterministic recipe; bump version when its meaning changes.

        compute(changeset, parent_values, key) must return JSON data. ``parents``
        is 'all', 'first', or a deterministic callable(changeset, key) returning
        prerequisite changeset IDs. Recipes execute locally, never from HTTP
        input. Eager recipes run with key=None after each changeset creation.
        Existing persisted values survive registration and process restarts.
        """
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise SCNError("Invalid derivation name")
        if not isinstance(version, (str, int)) or isinstance(version, bool) or not str(version):
            raise SCNError("Derivation version must be a nonempty string or integer")
        if not callable(compute) or not (parents in ("all", "first") or callable(parents)):
            raise SCNError("Invalid derivation recipe")
        if name in ("graph", "blame") and name in self._derivations:
            raise SCNError("Built-in derivations cannot be replaced")
        self._derivations[name] = {"version": str(version), "compute": compute,
                                   "parents": parents, "eager": bool(eager)}

    @staticmethod
    def _derived_json(value):
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, UnicodeError):
            raise SCNError("Derived values and keys must be JSON serializable") from None

    def _read_derived(self, name, version, oid, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM derived_values WHERE name=? AND version=? AND changeset=? AND cache_key=?",
                             (name, version, oid, key)).fetchone()
        return (True, json.loads(row[0])) if row else (False, None)

    def _write_derived(self, name, version, change, key, value):
        encoded = self._derived_json(value)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO derived_values VALUES (?,?,?,?,?)",
                       (name, version, change["id"], key, encoded))
            stored = db.execute("SELECT value FROM derived_values WHERE name=? AND version=? AND changeset=? AND cache_key=?",
                                (name, version, change["id"], key)).fetchone()[0]
            if stored != encoded:
                raise SCNError("Derivation is not deterministic; use a new recipe version", 500)
            if name == "graph" and version == "1" and key == "null":
                db.execute("INSERT OR REPLACE INTO graph_nodes VALUES (?,?,?,?,?)",
                           (change["id"], value["generation"], value["first_depth"],
                            self._derived_json(value["jumps"]), self._derived_json(change["parents"])))
                db.executemany("INSERT OR IGNORE INTO path_changes VALUES (?,?)",
                               ((path, change["id"]) for path in change["changes"]))
        return json.loads(encoded)

    def derive(self, name, rev, key=None):
        """Evaluate a versioned recipe parent-first, using durable cached values.

        An explicit stack handles long histories without Python recursion.
        A cache hit loads no changeset payload or ancestors. Legacy databases
        are backfilled lazily from immutable changesets on the first query.
        """
        if name not in self._derivations:
            raise SCNError("Unknown derivation: " + str(name))
        recipe = self._derivations[name]
        encoded_key = self._derived_json(key)
        target = self.resolve(rev)
        values, active = {}, set()
        stack = [(target, None, None)]
        while stack:
            oid, change, dependencies = stack.pop()
            if oid in values:
                continue
            if change is None:
                found, value = self._read_derived(name, recipe["version"], oid, encoded_key)
                if found:
                    values[oid] = value
                    continue
                if oid in active:
                    raise SCNError("Derivation dependencies contain a cycle")
                change = self.get_changeset(oid)
                selector = recipe["parents"]
                dependencies = (selector(change, key) if callable(selector)
                                else change["parents"][:1] if selector == "first" else change["parents"])
                if not isinstance(dependencies, list):
                    raise SCNError("Derivation dependencies must be a list")
                for parent in dependencies:
                    validate_oid(parent)
                active.add(oid)
                stack.append((oid, change, dependencies))
                stack.extend((parent, None, None) for parent in reversed(dependencies) if parent not in values)
            else:
                value = recipe["compute"](change, [values[p] for p in dependencies], key)
                values[oid] = self._write_derived(name, recipe["version"], change, encoded_key, value)
                active.remove(oid)
        return values[target]

    def clear_derived(self, name=None):
        """Discard rebuildable caches only; changesets, objects, and refs survive.

        Run administrative rebuilds while writers are stopped. Registered
        recipe versions share the same name and are all discarded together.
        """
        with self._connect() as db:
            if name is None:
                db.execute("DELETE FROM derived_values")
            else:
                db.execute("DELETE FROM derived_values WHERE name=?", (name,))
            if name is None or name == "graph":
                db.execute("DELETE FROM path_changes")
                db.execute("DELETE FROM graph_nodes")

    def rebuild_derived(self, name, rev, key=None):
        """Clear every cached version of one recipe, then rebuild through rev."""
        if name not in self._derivations:
            raise SCNError("Unknown derivation: " + str(name))
        self.clear_derived(name)
        return self.derive(name, rev, key)

    def _compute_graph(self, change, parents, key):
        jumps = change["parents"][:1]
        level = 1
        while jumps:
            previous = parents[0] if level == 1 else self._graph(jumps[-1])
            if len(previous["jumps"]) < level:
                break
            jumps.append(previous["jumps"][level - 1])
            level += 1
        return {"generation": 1 + max((p["generation"] for p in parents), default=0),
                "first_depth": 1 + (parents[0]["first_depth"] if parents else 0), "jumps": jumps}

    def _graph(self, oid):
        with self._connect() as db:
            row = db.execute("SELECT generation, first_depth, jumps, parents FROM graph_nodes WHERE id=?", (oid,)).fetchone()
        if not row:
            self.derive("graph", oid)
            return self._graph(oid)
        return {"generation": row[0], "first_depth": row[1], "jumps": json.loads(row[2]), "parents": json.loads(row[3])}

    def first_parent_ancestor(self, rev, distance):
        """Return the ancestor distance first-parent edges away, or None.

        After graph backfill this takes O(log distance) indexed graph reads.
        It does not claim logarithmic ancestry queries on an arbitrary DAG.
        """
        if not isinstance(distance, int) or isinstance(distance, bool) or distance < 0:
            raise SCNError("Ancestor distance must be a nonnegative integer")
        oid = self.resolve(rev)
        graph = self._graph(oid)
        if distance >= graph["first_depth"]:
            return None
        while distance:
            level = distance.bit_length() - 1
            oid = graph["jumps"][level]
            distance -= 1 << level
            if distance:
                graph = self._graph(oid)
        return oid

    def _on_first_parent_chain(self, candidate, descendant, candidate_depth, descendant_depth):
        if candidate_depth > descendant_depth:
            return False
        return self.first_parent_ancestor(descendant, descendant_depth - candidate_depth) == candidate

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
        for name, recipe in list(self._derivations.items()):
            if recipe["eager"]:
                self.derive(name, oid)
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
        if path is not None:
            return [self.get_changeset(oid) for oid in self._path_history(current, path, limit)]
        while current and len(result) < limit:
            change = self.get_changeset(current)
            if path is None or any(p == path or p.startswith(path + "/") or path.startswith(p + "/") for p in change["changes"]):
                result.append(change)
            current = change["parents"][0] if change["parents"] else None
        return result

    def _path_history(self, rev, path, limit=None):
        """Index candidate touches, then filter to this first-parent chain.

        The path index finds exact paths, ancestor directory tombstones, and
        descendant paths for directory queries. Binary lifting excludes touches
        on unrelated branches without loading their changeset payloads.
        """
        graph = self._graph(rev)
        if limit == 0:
            return []
        parts = path.split("/")
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        placeholders = ",".join("?" for _ in ancestors)
        sql = f"""SELECT DISTINCT p.changeset, g.first_depth
                  FROM path_changes p JOIN graph_nodes g ON g.id=p.changeset
                  WHERE (p.path IN ({placeholders}) OR (p.path>=? AND p.path<?))
                    AND g.first_depth<=? AND g.generation<=?
                  ORDER BY g.first_depth DESC, p.changeset"""
        result = []
        with self._connect() as db:
            candidates = db.execute(sql, [*ancestors, path + "/", path + "0",
                                          graph["first_depth"], graph["generation"]])
            for oid, depth in candidates:
                if self._on_first_parent_chain(oid, rev, depth, graph["first_depth"]):
                    result.append(oid)
                    if limit is not None and len(result) == limit:
                        break
        return result

    @staticmethod
    def _diff_manifests(before, after):
        return [{"path": p, "before": before.get(p), "after": after.get(p)}
                for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]

    def diff(self, base, target):
        before = self.get_changeset(base)["root"]
        after = self.get_changeset(target)["root"]
        changes = []

        def plain(entry):
            return {"oid": entry["oid"], "executable": entry["executable"]} if entry else None

        def walk(left, right, prefix):
            if left == right:
                return
            a = self.get_tree(left) if left else {}
            b = self.get_tree(right) if right else {}
            for name in sorted(a.keys() | b.keys()):
                old, new = a.get(name), b.get(name)
                if old == new:
                    continue
                path = prefix + name
                old_tree = old and old["kind"] == "tree"
                new_tree = new and new["kind"] == "tree"
                if old_tree or new_tree:
                    walk(old["oid"] if old_tree else None, new["oid"] if new_tree else None, path + "/")
                    if old and not old_tree:
                        changes.append({"path": path, "before": plain(old), "after": None})
                    if new and not new_tree:
                        changes.append({"path": path, "before": None, "after": plain(new)})
                else:
                    changes.append({"path": path, "before": plain(old), "after": plain(new)})
        walk(before, after, "")
        return sorted(changes, key=lambda change: change["path"])

    def _ancestors(self, rev):
        distances = {}
        queue = deque([(self.resolve(rev), 0)])
        while queue:
            current, distance = queue.popleft()
            if current in distances:
                continue
            distances[current] = distance
            queue.extend((p, distance + 1) for p in self._graph(current)["parents"])
        return distances

    def is_ancestor(self, ancestor, descendant):
        ancestor, descendant = self.resolve(ancestor), self.resolve(descendant)
        wanted, start = self._graph(ancestor), self._graph(descendant)
        if self._on_first_parent_chain(ancestor, descendant, wanted["first_depth"], start["first_depth"]):
            return True
        visited, pending = set(), [descendant]
        while pending:
            oid = pending.pop()
            if oid == ancestor:
                return True
            if oid in visited:
                continue
            visited.add(oid)
            graph = self._graph(oid)
            if graph["generation"] > wanted["generation"]:
                pending.extend(graph["parents"])
        return False

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

    def _file_entry(self, root, path):
        parts = path.split("/")
        for i, part in enumerate(parts):
            entry = self.get_tree(root).get(part)
            if entry is None:
                return None
            if i == len(parts) - 1:
                return entry if entry["kind"] == "blob" else None
            if entry["kind"] != "tree":
                return None
            root = entry["oid"]
        return None

    def _blame_parents(self, change, path):
        validate_path(path)
        if not change["parents"]:
            return []
        return self._path_history(change["parents"][0], path, 1)

    def _compute_blame(self, change, parents, path):
        entry = self._file_entry(change["root"], path)
        if entry is None:
            return {"exists": False, "text": True, "lines": []}
        try:
            lines = self.get_blob(entry["oid"]).decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return {"exists": True, "text": False, "lines": []}
        previous = parents[0]["lines"] if parents else []
        old_lines = [line["text"] for line in previous]
        updated = [{"text": text, "changeset": change["id"], "author": change["author"]} for text in lines]
        for block in SequenceMatcher(None, old_lines, lines, autojunk=False).get_matching_blocks():
            updated[block.b:block.b + block.size] = previous[block.a:block.a + block.size]
        return {"exists": True, "text": True, "lines": updated}

    def blame(self, rev, path):
        validate_path(path)
        result = self.derive("blame", rev, path)
        if not result["exists"]:
            raise SCNError("File not found", 404)
        if not result["text"]:
            raise SCNError("Blame requires a UTF-8 text file")
        return [dict(line, line=i + 1) for i, line in enumerate(result["lines"])]

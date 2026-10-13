"""HTTP client, bounded immutable-object cache, and sparse working copies."""
from __future__ import annotations

import hashlib
import fnmatch
import fcntl
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .errors import SCNError

DEFAULT_CACHE_BYTES = 256 * 1024 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _oid(kind, data):
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or len(value.split("/")) > 128 or "\\" in value
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(p.casefold() in ("", ".", "..", ".openscn", ".git") for p in value.split("/"))):
        raise SCNError(f"Unsafe repository path: {value!r}")
    return value


def _prefixes(paths):
    if not paths or "." in paths:
        return []
    return sorted(set(_path(p.rstrip("/")) for p in paths))


def _selected(path, prefixes):
    return not prefixes or any(path == p or path.startswith(p + "/") for p in prefixes)


def _relevant(path, prefixes):
    return _selected(path, prefixes) or any(p.startswith(path + "/") for p in prefixes)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + "-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mutating(function):
    @wraps(function)
    def locked(self, *args, **kwargs):
        with self._lock():
            return function(self, *args, **kwargs)
    return locked


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SCNError("Server redirected the request; use its canonical URL", 502)


class HTTPClient:
    def __init__(self, url, cache_dir=None, max_cache_bytes=DEFAULT_CACHE_BYTES):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SCNError("Server URL must be http(s), without credentials, query, or fragment")
        if max_cache_bytes < 0:
            raise SCNError("Cache byte limit must be nonnegative")
        self.url = url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.max_cache_bytes = max_cache_bytes
        self.opener = urllib.request.build_opener(_NoRedirect())
        if self.cache_dir is not None:
            if self.cache_dir.is_symlink():
                raise SCNError("Object cache must not be a symlink")
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def request(self, method, path, data=None, raw=False):
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if token := os.environ.get("OPENSCN_TOKEN"):
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if isinstance(data, bytes) else "application/json"
            if not isinstance(data, bytes):
                data = _json(data)
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=60) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
            except (ValueError, UnicodeError):
                error = {"error": f"HTTP {exc.code}: {exc.reason}"}
            raise SCNError(error.get("error", str(exc)), exc.code, error.get("details")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SCNError(f"Cannot reach {self.url}: {exc}", 503) from exc
        if raw:
            return body
        try:
            return json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise SCNError("Server returned invalid JSON", 502) from exc

    def _object(self, kind, oid):
        if not isinstance(oid, str) or len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
            raise SCNError("Invalid object ID")
        cache = self.cache_dir / f"{kind}-{oid}" if self.cache_dir is not None else None
        if cache is not None and cache.is_symlink():
            raise SCNError("Cached object must not be a symlink")
        if cache is not None and cache.is_file():
            data = cache.read_bytes()
            if _oid(kind, data) == oid:
                os.utime(cache, None)
                return data
            cache.unlink()
        if kind == "blob":
            data = self.request("GET", "/v1/blobs/" + oid, raw=True)
        else:
            data = _json(self.request("GET", "/v1/trees/" + oid)["entries"])
        if _oid(kind, data) != oid:
            raise SCNError(f"Server returned corrupt {kind} object {oid}", 502)
        if cache is not None and len(data) <= self.max_cache_bytes:
            fd, temporary = tempfile.mkstemp(dir=self.cache_dir, prefix=".incoming-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, cache)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            files = sorted((p for p in self.cache_dir.iterdir() if not p.is_symlink() and p.is_file() and p.name.startswith(("blob-", "tree-"))), key=lambda p: p.stat().st_mtime_ns)
            total = sum(p.stat().st_size for p in files)
            for old in files:
                if total <= self.max_cache_bytes:
                    break
                total -= old.stat().st_size
                old.unlink(missing_ok=True)
        return data

    def get_blob(self, oid):
        return self._object("blob", oid)

    def put_blob(self, data):
        oid = self.request("PUT", "/v1/blobs", data)["oid"]
        if oid != _oid("blob", data):
            raise SCNError("Server returned an incorrect blob ID", 502)
        return oid

    def get_changeset(self, rev):
        return self.request("GET", "/v1/changesets/" + urllib.parse.quote(rev, safe=""))

    def refs(self):
        return self.request("GET", "/v1/refs")["refs"]

    def manifest(self, rev, prefixes=None):
        prefixes = _prefixes(prefixes)
        changeset = self.get_changeset(rev)
        files = {}
        def walk(oid, directory=""):
            entries = json.loads(self._object("tree", oid))
            for name, entry in entries.items():
                if "/" in name:
                    raise SCNError("Invalid tree entry name", 502)
                path = _path(directory + name)
                if not _relevant(path, prefixes):
                    continue
                if entry["kind"] == "tree":
                    walk(entry["oid"], path + "/")
                elif entry["kind"] == "blob" and _selected(path, prefixes):
                    files[path] = {"oid": entry["oid"], "executable": bool(entry.get("executable", False))}
                elif entry["kind"] != "blob":
                    raise SCNError("Unsupported tree entry kind", 502)
        walk(changeset["root"])
        return changeset, files


class Workspace:
    def __init__(self, root, metadata):
        self.root = Path(root)
        self.metadata = metadata
        self.client = HTTPClient(metadata["server"], self.root / ".openscn" / "cache", metadata.get("cache_bytes", DEFAULT_CACHE_BYTES))

    @classmethod
    def open(cls, path="."):
        root = Path(path).resolve()
        for candidate in (root, *root.parents):
            if (candidate / ".openscn").is_symlink():
                raise SCNError("Workspace metadata must not be a symlink")
            metadata = candidate / ".openscn" / "workspace.json"
            if metadata.is_symlink():
                raise SCNError("Workspace metadata must not be a symlink")
            if metadata.is_file():
                try:
                    return cls(candidate, json.loads(metadata.read_text()))
                except (ValueError, KeyError) as exc:
                    raise SCNError("Invalid workspace metadata") from exc
        raise SCNError("Not in an OpenSCN workspace; run openscn clone first")

    @classmethod
    def clone(cls, url, directory, rev="main", sparse=None, cache_bytes=DEFAULT_CACHE_BYTES):
        root = Path(directory).absolute()
        if root.is_symlink():
            raise SCNError("Clone destination must not be a symlink")
        root = root.resolve()
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise SCNError("Clone destination must be empty")
        client = HTTPClient(url)
        refs = client.refs()
        prefixes = _prefixes(sparse)
        empty = not refs and rev == "main"
        if not empty and rev not in refs:
            client.get_changeset(rev)  # Validate explicit object IDs before creating files.
        root.mkdir(parents=True, exist_ok=True)
        (root / ".openscn").mkdir(mode=0o700)
        metadata = {"version": 1, "server": client.url, "head": None, "ref": rev if rev in refs or empty else "main", "refs": refs, "sparse": prefixes, "files": {}, "cache_bytes": cache_bytes}
        workspace = cls(root, metadata)
        workspace._save()
        if not empty:
            workspace.update(rev)
        return workspace

    @property
    def _journal_path(self):
        return self.root / ".openscn" / "checkout.json"

    def _pending_checkout(self):
        if self._journal_path.is_symlink():
            raise SCNError("Checkout journal must not be a symlink")
        return self._journal_path.exists()

    @contextmanager
    def _lock(self, recovery=False):
        folder = self.root / ".openscn"
        if folder.is_symlink():
            raise SCNError("Workspace metadata must not be a symlink")
        fd = os.open(folder / "workspace.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SCNError("Workspace lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SCNError("Another OpenSCN command is modifying this workspace; retry when it finishes", 409) from None
            metadata_path = folder / "workspace.json"
            if metadata_path.is_symlink():
                raise SCNError("Workspace metadata must not be a symlink")
            try:
                metadata = json.loads(metadata_path.read_text())
                changed_client = metadata["server"] != self.client.url or metadata.get("cache_bytes", DEFAULT_CACHE_BYTES) != self.client.max_cache_bytes
                self.metadata = metadata
                if changed_client:
                    self.client = HTTPClient(metadata["server"], folder / "cache", metadata.get("cache_bytes", DEFAULT_CACHE_BYTES))
            except (ValueError, KeyError) as exc:
                raise SCNError("Invalid workspace metadata") from exc
            if self._pending_checkout() and not recovery:
                raise SCNError("An interrupted checkout needs recovery; run openscn recover. Local edits will be checked before any file is replaced", 409)
            yield
        finally:
            os.close(fd)  # Closing also releases flock, including on exceptions.

    def _save(self):
        _atomic_json(self.root / ".openscn" / "workspace.json", self.metadata)

    def _file_entry(self, path):
        target = self._safe(path)
        try:
            info = target.stat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISDIR(info.st_mode):
            return "directory"
        if not stat.S_ISREG(info.st_mode):
            raise SCNError(f"Unsupported special file: {path}")
        digest = hashlib.sha256(b"blob\0")
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"oid": digest.hexdigest(), "executable": bool(info.st_mode & 0o111)}

    def _safe(self, path):
        _path(path)
        current = self.root
        for part in path.split("/"):
            current = current / part
            if current.is_symlink():
                raise SCNError(f"Symlink traversal is unsupported: {path}")
        return current

    def _ignore_patterns(self):
        path = self._safe(".openscnignore")
        if not path.is_file():
            return []
        patterns = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                raise SCNError(".openscnignore does not support negation (!)")
            patterns.append(line)
        return patterns

    @staticmethod
    def _ignored(path, is_directory, patterns):
        parts = path.split("/")
        for pattern in patterns:
            directories_only = pattern.endswith("/")
            pattern = pattern.rstrip("/")
            for index in range(1, len(parts) + 1):
                if directories_only and index == len(parts) and not is_directory:
                    continue
                candidate = "/".join(parts[:index]) if "/" in pattern else parts[index - 1]
                if fnmatch.fnmatchcase(candidate, pattern):
                    return True
        return False

    def _working(self):
        files = {}
        prefixes = self.metadata["sparse"]
        patterns = self._ignore_patterns()
        tracked = self.metadata["files"]
        tracked_directories = {"/".join(path.split("/")[:index]) for path in tracked for index in range(1, len(path.split("/")))}
        def walk(folder, directory=""):
            for child in sorted(folder.iterdir()):
                if child.name.casefold() in (".openscn", ".git"):
                    continue
                path = directory + child.name
                if not _relevant(path, prefixes):
                    continue
                _path(path)
                needed = path in tracked or path in tracked_directories
                if not needed and self._ignored(path, child.is_dir(), patterns):
                    continue
                if child.is_symlink():
                    raise SCNError(f"Symlinks are unsupported: {path}")
                info = child.stat()
                if stat.S_ISDIR(info.st_mode):
                    walk(child, path + "/")
                elif stat.S_ISREG(info.st_mode) and _selected(path, prefixes):
                    files[path] = self._file_entry(path)
                elif not stat.S_ISREG(info.st_mode):
                    raise SCNError(f"Unsupported special file: {path}")
        walk(self.root)
        return files

    def status(self):
        if self._pending_checkout():
            raise SCNError("An interrupted checkout needs recovery; run openscn recover", 409)
        working = self._working()
        baseline = self.metadata["files"]
        return [{"path": path, "status": "added" if path not in baseline else "deleted" if path not in working else "modified", "before": baseline.get(path), "after": working.get(path)} for path in sorted(baseline.keys() | working.keys()) if baseline.get(path) != working.get(path)]

    @_mutating
    def commit(self, message, author):
        if not message.strip():
            raise SCNError("Commit message must not be empty")
        statuses = self.status()
        if not statuses:
            raise SCNError("No changes to commit")
        changes = {}
        for change in statuses:
            entry = change["after"]
            if entry is not None:
                data = self._safe(change["path"]).read_bytes()
                if _oid("blob", data) != entry["oid"]:
                    raise SCNError("A file changed while committing; retry")
                self.client.put_blob(data)
            changes[change["path"]] = entry
        changeset = self.client.request("POST", "/v1/changesets", {"parents": [self.metadata["head"]] if self.metadata["head"] else [], "changes": changes, "message": message, "author": author})
        files = dict(self.metadata["files"])
        for path, entry in changes.items():
            if entry is None:
                files.pop(path, None)
            else:
                files[path] = entry
        self.metadata.update(head=changeset["id"], files=files)
        self._save()
        return changeset

    def _validate_checkout(self, files):
        # Refuse aliases even on a case-sensitive filesystem, so this working
        # copy cannot silently lose files when used on macOS's default volume.
        aliases = {}
        for path in files:
            _path(path)
            parts = path.split("/")
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                folded = unicodedata.normalize("NFC", prefix).casefold()
                if folded in aliases and aliases[folded] != prefix:
                    raise SCNError(f"Case or Unicode path collision: {aliases[folded]} and {prefix}")
                aliases[folded] = prefix
        old = self.metadata["files"]
        statuses = self.status()
        dirty = [s["path"] for s in statuses if s["status"] != "added"]
        if dirty:
            raise SCNError("Update would overwrite local changes: " + ", ".join(dirty), 409)
        for path in files:
            target = self._safe(path)
            if target.exists() and path not in old:
                if not target.is_dir():
                    raise SCNError(f"Untracked file would be overwritten: {path}", 409)
                for child in target.rglob("*"):
                    relative = child.relative_to(self.root).as_posix()
                    if child.is_symlink() or (not child.is_dir() and relative not in old):
                        raise SCNError(f"Untracked path would be overwritten: {relative}", 409)
            for parent in target.parents:
                if parent == self.root:
                    break
                if parent.exists() and not parent.is_dir() and parent.relative_to(self.root).as_posix() not in old:
                    raise SCNError(f"Untracked file blocks directory: {parent.relative_to(self.root)}", 409)

    def _checkout(self, changeset, files, metadata_changes=None):
        self._validate_checkout(files)
        old = self.metadata["files"]
        staging = Path(tempfile.mkdtemp(dir=self.root / ".openscn", prefix="checkout-stage-"))
        journal_created = False
        try:
            operations = [{"path": path, "before": old[path], "after": None} for path in sorted(old.keys() - files.keys(), key=lambda p: (-p.count("/"), p))]
            for path, entry in sorted(files.items()):
                if old.get(path) == entry:
                    continue
                name = str(len(operations)) + ".blob"
                # One verified blob is held at a time, independent of checkout size.
                with (staging / name).open("xb") as stream:
                    stream.write(self.client.get_blob(entry["oid"]))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(staging / name, 0o755 if entry["executable"] else 0o644)
                operations.append({"path": path, "before": old.get(path), "after": entry, "staged": name})
            _sync_directory(staging)
            # Editors do not participate in our lock. Check again after fetching.
            self._validate_checkout(files)
            after = {**self.metadata, **(metadata_changes or {}), "head": changeset["id"], "files": files}
            journal = {"version": 1, "before": self.metadata, "after": after, "staging": staging.name, "operations": operations, "next": 0}
            _atomic_json(self._journal_path, journal)
            journal_created = True
            self._resume_checkout(journal)
        finally:
            if not journal_created and not self._journal_path.exists():
                shutil.rmtree(staging)

    def _apply_checkout_operation(self, operation, staging):
        path = operation["path"]
        target = self._safe(path)
        current = self._file_entry(path)
        before, after = operation["before"], operation["after"]
        if current == after:
            return  # A crash may happen after rename/unlink but before journal progress.
        if current != before and not (current == "directory" and before is None and after is not None):
            raise SCNError(f"Checkout stopped to preserve local edits at {path}. Save or move those edits outside the workspace, restore the original file if needed, then run openscn recover", 409)
        if after is None:
            target.unlink()
            _sync_directory(target.parent)
            parent = target.parent
            while parent != self.root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                _sync_directory(parent.parent)
                parent = parent.parent
            return
        source = staging / operation["staged"]
        if source.is_symlink() or not source.is_file():
            raise SCNError("Checkout staging is missing or unsafe; preserve the workspace and clone the target revision into a new directory", 409)
        digest = hashlib.sha256(b"blob\0")
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != after["oid"]:
            raise SCNError("Checkout staging failed integrity verification; preserve the workspace and clone the target revision into a new directory", 409)
        # Integrity checking can take time for a large blob; recheck edits made
        # during that read before replacing anything in the working copy.
        latest = self._file_entry(path)
        if latest == after:
            return
        if latest != current:
            raise SCNError(f"Checkout stopped to preserve local edits at {path}; save those edits outside the workspace and run openscn recover", 409)
        if target.is_dir():
            def remove_empty(directory):
                for child in directory.iterdir():
                    if child.is_symlink() or not child.is_dir():
                        raise SCNError(f"Untracked path blocks checkout: {child.relative_to(self.root)}; preserve it elsewhere, then run openscn recover", 409)
                    remove_empty(child)
                directory.rmdir()
            remove_empty(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._safe(path)
        os.chmod(source, 0o755 if after["executable"] else 0o644)
        os.replace(source, target)
        _sync_directory(target.parent)
        _sync_directory(staging)

    def _resume_checkout(self, journal):
        staging_name = journal["staging"]
        if not isinstance(staging_name, str) or not staging_name.startswith("checkout-stage-") or "/" in staging_name or "\\" in staging_name:
            raise SCNError("Invalid checkout staging path")
        staging = self.root / ".openscn" / staging_name
        if staging.is_symlink():
            raise SCNError("Checkout staging must not be a symlink")
        progress = staging / "progress.json"
        if progress.is_symlink():
            raise SCNError("Checkout progress must not be a symlink")
        next_operation = json.loads(progress.read_text())["next"] if progress.exists() else 0
        if not isinstance(next_operation, int) or not 0 <= next_operation <= len(journal["operations"]):
            raise SCNError("Invalid checkout progress")
        for index in range(next_operation, len(journal["operations"])):
            operation = journal["operations"][index]
            if operation.get("staged") != str(index) + ".blob" and operation["after"] is not None:
                raise SCNError("Invalid staged object path")
            self._apply_checkout_operation(operation, staging)
            _atomic_json(progress, {"next": index + 1})
        self.metadata = journal["after"]
        self._save()
        self._journal_path.unlink()
        _sync_directory(self._journal_path.parent)
        shutil.rmtree(staging, ignore_errors=True)
        return self.metadata["head"]

    def recover(self):
        """Continue an interrupted checkout without replacing conflicting edits."""
        with self._lock(recovery=True):
            if not self._pending_checkout():
                raise SCNError("No interrupted checkout to recover")
            try:
                journal = json.loads(self._journal_path.read_text())
                if journal["version"] != 1 or not isinstance(journal["next"], int) or not 0 <= journal["next"] <= len(journal["operations"]):
                    raise ValueError("invalid journal")
                if self.metadata != journal["before"] and self.metadata != journal["after"]:
                    raise SCNError("Workspace metadata changed after checkout was interrupted; preserve it and clone the target revision into a new directory", 409)
                return self._resume_checkout(journal)
            except (ValueError, KeyError, TypeError) as exc:
                raise SCNError("Invalid checkout journal; preserve the workspace and clone the target revision into a new directory") from exc

    @_mutating
    def update(self, rev=None):
        refs = self.client.refs()
        rev = rev or self.metadata["ref"]
        changeset, files = self.client.manifest(refs.get(rev, rev), self.metadata["sparse"])
        metadata_changes = {"refs": refs}
        if rev in refs:
            metadata_changes["ref"] = rev
        self._checkout(changeset, files, metadata_changes)
        return changeset

    @_mutating
    def land(self, ref=None):
        ref = ref or self.metadata["ref"]
        if self.metadata["head"] is None:
            raise SCNError("Nothing to land; create a commit first")
        expected = self.metadata["refs"].get(ref)
        result = self.client.request("POST", "/v1/refs/" + urllib.parse.quote(ref, safe=""), {"target": self.metadata["head"], "expected": expected})
        self.metadata["refs"][ref] = self.metadata["head"]
        self.metadata["ref"] = ref
        self._save()
        return result

    @_mutating
    def integrate(self, operation, rev, message, author):
        if self.metadata["head"] is None:
            raise SCNError("Create or check out a changeset first")
        if self.status():
            raise SCNError("Commit local changes before merging or rebasing", 409)
        refs = self.client.refs()
        target = refs.get(rev, rev)
        body = {"left": self.metadata["head"], "right": target} if operation == "merge" else {"rev": self.metadata["head"], "onto": target}
        body.update(message=message, author=author)
        result = self.client.request("POST", "/v1/" + operation, body)
        changeset, files = self.client.manifest(result["id"], self.metadata["sparse"])
        self._checkout(changeset, files, {"refs": refs})
        return result


def import_git(path, server, rev="HEAD", target="main"):
    """Import the reachable Git DAG in parent order, then CAS-land its tip."""
    directory = Path(path).absolute()
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(directory), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        except FileNotFoundError as exc:
            raise SCNError("Git is required for import-git") from exc
        except subprocess.CalledProcessError as exc:
            raise SCNError(exc.stderr.decode("utf-8", "replace").strip()) from exc
    tip = git("rev-parse", "--verify", "--end-of-options", rev + "^{commit}").decode().strip()
    commits = git("rev-list", "--reverse", "--topo-order", tip).decode().splitlines()
    client = HTTPClient(server)
    refs = client.refs()
    mapping = {}
    blobs = {}
    for commit in commits:
        raw = git("show", "-s", "--format=%P%x00%an <%ae>%x00%aI%x00%B", commit)
        parents, author, timestamp, message = raw.decode("utf-8", "replace").split("\0", 3)
        parents = parents.split()
        diff_args = ["diff-tree", "--no-commit-id", "--no-abbrev", "--no-renames", "-r", "--raw", "-z"]
        diff_args += [parents[0], commit] if parents else ["--root", commit]
        rows = git(*diff_args).split(b"\0")
        changes = {}
        for index in range(0, len(rows) - 1, 2):
            header, name = rows[index:index + 2]
            old_mode, mode, old_oid, git_oid, status = header.decode("ascii").split()
            try:
                name = name.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SCNError("Git paths must be UTF-8") from exc
            _path(name)
            if mode == "000000":
                changes[name] = None
                continue
            if mode not in ("100644", "100755"):
                raise SCNError(f"Unsupported Git entry {name}: symlinks and submodules cannot be imported")
            if git_oid not in blobs:
                blobs[git_oid] = client.put_blob(git("cat-file", "blob", git_oid))
            changes[name] = {"oid": blobs[git_oid], "executable": mode == "100755"}
        imported = client.request("POST", "/v1/changesets", {"parents": [mapping[p] for p in parents], "changes": changes, "message": message.rstrip("\n") or "Imported Git commit", "author": author, "timestamp": timestamp})
        mapping[commit] = imported["id"]
    client.request("POST", "/v1/refs/" + urllib.parse.quote(target, safe=""), {"target": mapping[tip], "expected": refs.get(target)})
    return {"commits": len(mapping), "head": mapping[tip], "ref": target}

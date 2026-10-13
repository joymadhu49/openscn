"""Bounded, disposable HTTP cache using the same object API as the server."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

from . import __version__
from .errors import SCNError
from .server import Handler, MAX_BODY, Server

OBJECT = re.compile(r"^/v1/(blobs|trees)/([0-9a-f]{64})$")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ObjectCache:
    """Process-local locking with atomic files; safe to discard at any time."""

    def __init__(self, directory, max_bytes=256 * 1024 * 1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        if max_bytes < 0:
            raise SCNError("Cache size cannot be negative")
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        with self.lock:
            self._trim()

    def _trim(self):
        files = []
        for path in self.directory.glob("*.z"):
            try:
                stat = path.stat()
                files.append((stat.st_mtime_ns, stat.st_size, path))
            except FileNotFoundError:
                continue
        size = sum(item[1] for item in files)
        for _, length, path in sorted(files):
            if size <= self.max_bytes:
                break
            path.unlink(missing_ok=True)
            size -= length

    def get(self, kind, oid):
        path = self.directory / f"{kind}-{oid}.z"
        with self.lock:
            try:
                data = zlib.decompress(path.read_bytes())
                verify(kind, oid, data)
                os.utime(path, None)
            except (OSError, zlib.error, SCNError):
                path.unlink(missing_ok=True)
                self.misses += 1
                return None
            self.hits += 1
            return data

    def put(self, kind, oid, data):
        verify(kind, oid, data)
        compressed = zlib.compress(data)
        if len(compressed) > self.max_bytes:
            return
        path = self.directory / f"{kind}-{oid}.z"
        with self.lock:
            fd, name = tempfile.mkstemp(prefix=".tmp-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(compressed)
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
            self._trim()


def verify(kind, oid, data):
    if kind == "trees":
        try:
            entries = json.loads(data)["entries"]
            hashed = b"tree\0" + json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        except (ValueError, KeyError, TypeError):
            raise SCNError("Upstream sent an invalid tree", 502) from None
    else:
        hashed = b"blob\0" + data
    if hashlib.sha256(hashed).hexdigest() != oid:
        raise SCNError("Object integrity check failed", 502)


class CacheServer(Server):
    def __init__(self, address, upstream, directory, max_bytes=256 * 1024 * 1024, token=None, upstream_token=None):
        parsed = urlsplit(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise SCNError("Upstream must be an HTTP(S) server origin without credentials")
        self.upstream = upstream.rstrip("/")
        namespace = hashlib.sha256(self.upstream.encode()).hexdigest()[:16]
        self.cache = ObjectCache(Path(directory) / namespace, max_bytes)
        self.upstream_token = upstream_token if upstream_token is not None else token
        self.opener = build_opener(NoRedirect)
        super().__init__(address, None, token)
        self.RequestHandlerClass = CacheHandler


class CacheHandler(Handler):
    def dispatch(self):
        route = urlsplit(self.path)
        if self.path == "/health" and self.command == "GET":
            return self.reply(200, {"status": "ok", "version": __version__, "role": "cache"})
        self.authorize()
        if not route.path.startswith("/v1/") or route.scheme or route.netloc or route.fragment:
            raise SCNError("Unknown endpoint", 404)
        match = OBJECT.fullmatch(route.path) if self.command == "GET" and not route.query else None
        if match:
            kind, oid = match.groups()
            data = self.server.cache.get(kind, oid)
            if data is not None:
                return self.reply(200, data if kind == "blobs" else json.loads(data), raw=kind == "blobs", immutable=True)
        body = self.body(raw=True) if self.command in ("PUT", "POST") else None
        headers = {"Content-Type": self.headers.get("Content-Type", "application/octet-stream")}
        if self.server.upstream_token:
            headers["Authorization"] = "Bearer " + self.server.upstream_token
        request = Request(self.server.upstream + self.path, data=body, headers=headers, method=self.command)
        try:
            with self.server.opener.open(request, timeout=30) as response:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise SCNError("Upstream response exceeds the 32 MiB limit", 502)
                if match:
                    self.server.cache.put(kind, oid, data)
                raw = response.headers.get_content_type() == "application/octet-stream"
                return self.reply(response.status, data if raw else json.loads(data), raw=raw, immutable=bool(match))
        except HTTPError as exc:
            # Forward structured errors only; don't expose an upstream proxy's HTML.
            try:
                payload = json.loads(exc.read(MAX_BODY))
            except (ValueError, UnicodeError):
                payload = {"error": "Upstream request failed"}
            return self.reply(exc.code, payload)
        except (URLError, TimeoutError, OSError):
            raise SCNError("Upstream unavailable", 502) from None
        except (ValueError, UnicodeError):
            raise SCNError("Upstream returned invalid JSON", 502) from None


def serve(upstream, data, host="127.0.0.1", port=7778, max_bytes=256 * 1024 * 1024):
    token = os.environ.get("OPENSCN_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SCNError("Set OPENSCN_TOKEN before binding a non-loopback address")
    server = CacheServer((host, port), upstream, data, max_bytes, token, os.environ.get("OPENSCN_UPSTREAM_TOKEN"))
    print(f"OpenSCN cache listening on http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

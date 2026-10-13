"""Bounded, disposable HTTP cache using the same object API as the server."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
import re
import tempfile
import threading
import time
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
        self.disk_errors = 0
        with self.lock:
            try:
                self._trim()
            except OSError:
                self.disk_errors += 1

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
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    self.disk_errors += 1
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
            name = None
            try:
                fd, name = tempfile.mkstemp(prefix=".tmp-", dir=self.directory)
                with os.fdopen(fd, "wb") as output:
                    output.write(compressed)
                os.replace(name, path)
                self._trim()
            except OSError:
                # Cache storage is disposable; a full disk must not turn a
                # successful authoritative read into a failed checkout.
                self.disk_errors += 1
            finally:
                if name:
                    try:
                        Path(name).unlink(missing_ok=True)
                    except OSError:
                        pass


class UpstreamGuard:
    """Bound upstream concurrency and stop repeated calls during an outage."""

    def __init__(self, limit=16, failure_threshold=3, cooldown=5.0, clock=time.monotonic):
        if limit < 1 or failure_threshold < 1 or cooldown <= 0:
            raise SCNError("Invalid upstream limits")
        self.limit = limit
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.clock = clock
        self.lock = threading.Lock()
        self.active = 0
        self.failures = 0
        self.open_until = 0.0
        self.probing = False
        self.requests = 0
        self.rejections = 0

    def enter(self):
        with self.lock:
            if self.active >= self.limit or self.probing or (self.open_until and self.clock() < self.open_until):
                self.rejections += 1
                raise SCNError("Upstream temporarily unavailable or at capacity; retry later", 503)
            probe = bool(self.open_until)
            self.probing = probe
            self.active += 1
            self.requests += 1
            return probe

    def finish(self, probe, healthy):
        with self.lock:
            self.active -= 1
            if probe:
                self.probing = False
                self.failures = 0 if healthy else self.failure_threshold
                self.open_until = 0.0 if healthy else self.clock() + self.cooldown
            elif not self.open_until:
                self.failures = 0 if healthy else self.failures + 1
                if self.failures >= self.failure_threshold:
                    self.open_until = self.clock() + self.cooldown

    def stats(self):
        with self.lock:
            return {"upstream_requests": self.requests, "upstream_inflight": self.active,
                    "upstream_limit": self.limit, "upstream_rejections": self.rejections,
                    "circuit_open": bool(self.open_until), "probe_inflight": self.probing}


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
    def __init__(self, address, upstream, directory, max_bytes=256 * 1024 * 1024, token=None, upstream_token=None,
                 max_upstream=16, failure_threshold=3, cooldown=5.0, max_workers=64):
        parsed = urlsplit(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise SCNError("Upstream must be an HTTP(S) server origin without credentials")
        self.upstream = upstream.rstrip("/")
        namespace = hashlib.sha256(self.upstream.encode()).hexdigest()[:16]
        self.cache = ObjectCache(Path(directory) / namespace, max_bytes)
        self.upstream_token = upstream_token if upstream_token is not None else token
        self.opener = build_opener(NoRedirect)
        self.guard = UpstreamGuard(max_upstream, failure_threshold, cooldown)
        self.flight_lock = threading.Lock()
        self.flights = {}
        self.coalesced = 0
        super().__init__(address, None, token, max_workers)
        self.RequestHandlerClass = CacheHandler

    def fetch(self, path, method="GET", body=None, content_type="application/octet-stream", object_key=None):
        headers = {"Content-Type": content_type}
        if self.upstream_token:
            headers["Authorization"] = "Bearer " + self.upstream_token
        request = Request(self.upstream + path, data=body, headers=headers, method=method)
        probe = self.guard.enter()
        healthy = False
        try:
            try:
                response = self.opener.open(request, timeout=30)
            except HTTPError as exc:
                response = exc
            with response:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise SCNError("Upstream response exceeds the 32 MiB limit", 502)
                raw = response.headers.get_content_type() == "application/octet-stream"
                status = response.code
                if status >= 400 or status < 200 or status >= 300:
                    try:
                        payload = json.loads(data)
                    except (ValueError, UnicodeError):
                        payload = {"error": "Upstream request failed"}
                    healthy = 400 <= status < 500 and status != 429
                    return status, payload, False
                if not raw:
                    try:
                        data = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise SCNError("Upstream returned invalid JSON", 502) from None
                if object_key:
                    kind, oid = object_key
                    if status != 200 or raw != (kind == "blobs"):
                        raise SCNError("Upstream returned an unexpected object type", 502)
                    verify(kind, oid, data if raw else json.dumps(data, ensure_ascii=False).encode())
                healthy = True
                return status, data, raw
        except (URLError, TimeoutError, OSError):
            raise SCNError("Upstream unavailable", 502) from None
        finally:
            self.guard.finish(probe, healthy)

    def object(self, kind, oid):
        def read_cache():
            try:
                return self.cache.get(kind, oid)
            except OSError:
                with self.cache.lock:
                    self.cache.disk_errors += 1
                return None

        data = read_cache()
        if data is not None:
            return 200, data if kind == "blobs" else json.loads(data), kind == "blobs"
        key = (kind, oid)
        with self.flight_lock:
            pending = self.flights.get(key)
            leader = pending is None
            if leader:
                # Recheck under the flight lock: another fetch may just have
                # completed between our first cache lookup and this lock.
                data = read_cache()
                if data is not None:
                    return 200, data if kind == "blobs" else json.loads(data), kind == "blobs"
                pending = Future()
                self.flights[key] = pending
            else:
                self.coalesced += 1
        if not leader:
            try:
                return pending.result(timeout=35)
            except FutureTimeout:
                raise SCNError("Timed out waiting for shared object fetch", 504) from None
        try:
            result = self.fetch(f"/v1/{kind}/{oid}", object_key=key)
            status, payload, raw = result
            if status == 200:
                if raw != (kind == "blobs"):
                    raise SCNError("Upstream returned an unexpected object type", 502)
                encoded = payload if raw else json.dumps(payload, ensure_ascii=False).encode()
                # Integrity is mandatory even if cache persistence fails.
                verify(kind, oid, encoded)
                try:
                    self.cache.put(kind, oid, encoded)
                except OSError:
                    with self.cache.lock:
                        self.cache.disk_errors += 1
            pending.set_result(result)
            return result
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self.flight_lock:
                self.flights.pop(key, None)

    def stats(self):
        with self.flight_lock:
            result = {"coalesced_requests": self.coalesced, "object_fetches_inflight": len(self.flights)}
        with self.cache.lock:
            result.update(hits=self.cache.hits, misses=self.cache.misses, disk_errors=self.cache.disk_errors)
        return dict(result, **self.guard.stats())


class CacheHandler(Handler):
    def dispatch(self):
        route = urlsplit(self.path)
        if self.path == "/health" and self.command == "GET":
            return self.reply(200, {"status": "ok", "version": __version__, "role": "cache"})
        self.authorize()
        if not route.path.startswith("/v1/") or route.scheme or route.netloc or route.fragment:
            raise SCNError("Unknown endpoint", 404)
        if route.path == "/v1/cache/stats" and self.command == "GET":
            return self.reply(200, self.server.stats())
        match = OBJECT.fullmatch(route.path) if self.command == "GET" and not route.query else None
        if match:
            kind, oid = match.groups()
            status, payload, raw = self.server.object(kind, oid)
            return self.reply(status, payload, raw=raw, immutable=status == 200)
        body = self.body(raw=True) if self.command in ("PUT", "POST") else None
        status, payload, raw = self.server.fetch(self.path, self.command, body, self.headers.get("Content-Type", "application/octet-stream"))
        return self.reply(status, payload, raw=raw)


def serve(upstream, data, host="127.0.0.1", port=7778, max_bytes=256 * 1024 * 1024,
          max_upstream=16, failure_threshold=3, cooldown=5.0, max_workers=64):
    token = os.environ.get("OPENSCN_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SCNError("Set OPENSCN_TOKEN before binding a non-loopback address")
    server = CacheServer((host, port), upstream, data, max_bytes, token, os.environ.get("OPENSCN_UPSTREAM_TOKEN"),
                         max_upstream, failure_threshold, cooldown, max_workers)
    print(f"OpenSCN cache listening on http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

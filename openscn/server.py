"""Small, self-hostable HTTP transport for OpenSCN's source-control API."""

from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .errors import SCNError
from .store import Store

LOG = logging.getLogger("openscn")
MAX_BODY = 32 * 1024 * 1024


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, token=None):
        self.store = store
        self.token = token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenSCN/" + __version__

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Do not log paths: repository names, filenames and refs can be sensitive.
        LOG.debug("HTTP request completed")

    def reply(self, status, payload, raw=False, immutable=False):
        data = payload if raw else json.dumps(payload, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if immutable else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self, raw=False):
        if self.headers.get("Transfer-Encoding"):
            raise SCNError("Chunked requests are not supported", 400)
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise SCNError("Invalid Content-Length", 400) from None
        if length < 0:
            raise SCNError("Content-Length is required", 411)
        if length > MAX_BODY:
            raise SCNError("Request exceeds the 32 MiB limit", 413)
        if not raw and self.headers.get_content_type() != "application/json":
            raise SCNError("Expected application/json", 415)
        data = self.rfile.read(length)
        if len(data) != length:
            raise SCNError("Incomplete request body", 400)
        if raw:
            return data
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise SCNError("Invalid JSON", 400) from None
        if not isinstance(obj, dict):
            raise SCNError("JSON body must be an object", 400)
        return obj

    @staticmethod
    def fields(body, required, optional=()):
        missing = set(required) - body.keys()
        extra = body.keys() - set(required) - set(optional)
        if missing or extra:
            raise SCNError("Invalid request fields", 400, {"missing": sorted(missing), "unknown": sorted(extra)})
        return body

    def authorize(self):
        if self.headers.get("Origin"):
            raise SCNError("Browser cross-origin access is disabled", 403)
        if self.server.token:
            actual = self.headers.get("Authorization", "")
            expected = "Bearer " + self.server.token
            if not hmac.compare_digest(actual.encode(), expected.encode()):
                raise SCNError("A valid bearer token is required", 401)
        else:
            # A hostile webpage can rebind its own hostname to loopback. Origin
            # alone does not cover same-origin browser GET requests in that case.
            host = urlsplit("//" + self.headers.get("Host", "")).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise SCNError("Unauthenticated requests require a loopback Host", 403)

    def dispatch(self):
        route = urlsplit(self.path)
        path = unquote(route.path)
        query = parse_qs(route.query, keep_blank_values=True)
        method = self.command
        store = self.server.store
        if path == "/health" and method == "GET":
            return self.reply(200, {"status": "ok", "version": __version__})
        self.authorize()

        def param(name, default=None):
            values = query.get(name)
            if values is None:
                if default is None:
                    raise SCNError("Missing query parameter: " + name)
                return default
            if len(values) != 1 or not values[0]:
                raise SCNError("Expected one nonempty query parameter: " + name)
            return values[0]

        if path == "/v1/refs" and method == "GET":
            return self.reply(200, {"refs": store.refs()})
        if path == "/v1/blobs" and method == "PUT":
            return self.reply(201, {"oid": store.put_blob(self.body(raw=True))})
        if path.startswith("/v1/blobs/") and method == "GET":
            return self.reply(200, store.get_blob(path[len("/v1/blobs/"):]), raw=True, immutable=True)
        if path.startswith("/v1/trees/") and method == "GET":
            return self.reply(200, {"entries": store.get_tree(path[len("/v1/trees/"):])}, immutable=True)
        if path == "/v1/changesets" and method == "POST":
            data = self.fields(self.body(), ("parents", "changes", "message", "author"), ("timestamp",))
            return self.reply(201, store.create_changeset(**data))
        if path.startswith("/v1/changesets/") and method == "GET":
            return self.reply(200, store.get_changeset(path[len("/v1/changesets/"):]))
        if path.startswith("/v1/refs/") and method == "POST":
            data = self.fields(self.body(), ("target", "expected"))
            if not isinstance(data["target"], str) or (data["expected"] is not None and not isinstance(data["expected"], str)):
                raise SCNError("target and expected must be changeset IDs; expected may be null")
            target = store.resolve(data["target"])
            expected = data["expected"]
            if expected is not None and not store.is_ancestor(expected, target):
                raise SCNError("Ref updates must be fast-forward; merge or rebase first", 409)
            return self.reply(200, store.update_ref(path[len("/v1/refs/"):], target, expected))
        if path.startswith("/v1/manifest/") and method == "GET":
            return self.reply(200, {"files": store.manifest(path[len("/v1/manifest/"):], query.get("prefix"))})
        if path.startswith("/v1/log/") and method == "GET":
            try:
                limit = int(param("limit", "50"))
            except ValueError:
                raise SCNError("limit must be an integer") from None
            if not 1 <= limit <= 1000:
                raise SCNError("limit must be between 1 and 1000")
            return self.reply(200, {"changesets": store.log(path[len("/v1/log/"):], limit, query.get("path", [None])[0])})
        if path == "/v1/diff" and method == "GET":
            return self.reply(200, {"changes": store.diff(param("base"), param("target"))})
        if path == "/v1/merge-base" and method == "GET":
            return self.reply(200, {"base": store.merge_base(param("left"), param("right"))})
        if path.startswith("/v1/blame/") and method == "GET":
            return self.reply(200, {"lines": store.blame(path[len("/v1/blame/"):], param("path"))})
        if path == "/v1/merge" and method == "POST":
            return self.reply(201, store.merge(**self.fields(self.body(), ("left", "right", "message", "author"))))
        if path == "/v1/rebase" and method == "POST":
            return self.reply(201, store.rebase(**self.fields(self.body(), ("rev", "onto", "message", "author"))))
        raise SCNError("Unknown endpoint or unsupported method", 404)

    def handle_request(self):
        try:
            self.dispatch()
        except SCNError as exc:
            self.reply(exc.status, {"error": str(exc), "details": exc.details})
        except (TypeError, ValueError, UnicodeError, RecursionError):
            self.reply(400, {"error": "Invalid request value"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            LOG.exception("Unhandled API error")
            self.reply(500, {"error": "Internal server error"})

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request


def serve(data, host="127.0.0.1", port=7777):
    token = os.environ.get("OPENSCN_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SCNError("Set OPENSCN_TOKEN before binding a non-loopback address")
    server = Server((host, port), Store(data), token)
    print(f"OpenSCN {__version__} listening on http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

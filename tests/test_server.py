import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openscn.cache import CacheServer, ObjectCache, UpstreamGuard
from openscn.errors import SCNError
from openscn.server import Server
from openscn.store import Store


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "store")
        self.server = Server(("127.0.0.1", 0), self.store, "test-token")
        self.url = self.start(self.server)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def request(self, path, data=None, method="GET", url=None, token="test-token", headers=None):
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
        request = Request((url or self.url) + path, data=data, method=method, headers=request_headers)
        with urlopen(request, timeout=5) as response:
            payload = response.read()
            return payload if response.headers.get_content_type() == "application/octet-stream" else json.loads(payload)

    def commit(self, parents, files, message="test"):
        changes = {path: None if content is None else {"oid": self.request("/v1/blobs", content, "PUT")["oid"], "executable": False} for path, content in files.items()}
        return self.request("/v1/changesets", {"parents": parents, "changes": changes, "message": message, "author": "tester"}, "POST")

    def assert_http_error(self, code, *args, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            self.request(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        payload = json.loads(caught.exception.read())
        self.assertIn("error", payload)
        caught.exception.close()
        return payload

    def test_auth_and_browser_origin(self):
        self.assertEqual(self.request("/health", token=None)["status"], "ok")
        self.assert_http_error(401, "/v1/refs", token=None)
        self.assert_http_error(401, "/v1/refs", token="wrong")
        self.assert_http_error(403, "/v1/refs", headers={"Origin": "https://example.org"})
        self.assertEqual(self.request("/v1/refs"), {"refs": {}})

    def test_unauthenticated_dns_rebinding_rejected(self):
        local = Server(("127.0.0.1", 0), self.store)
        url = self.start(local)
        self.assertEqual(self.request("/v1/refs", url=url, token=None), {"refs": {}})
        self.assert_http_error(403, "/v1/refs", url=url, token=None, headers={"Host": "attacker.example"})

    def test_end_to_end_branches_merge_and_history(self):
        first = self.commit([], {"src/a": b"one\ntwo\n", "docs/readme": b"hello\n"})
        self.request("/v1/refs/main", {"target": first["id"], "expected": None}, "POST")
        left = self.commit([first["id"]], {"src/a": b"one\nchanged\n"})
        right = self.commit([first["id"]], {"docs/readme": b"updated\n"})
        base = self.request(f'/v1/merge-base?left={left["id"]}&right={right["id"]}')
        self.assertEqual(base["base"], first["id"])
        merged = self.request("/v1/merge", {"left": left["id"], "right": right["id"], "message": "combine", "author": "tester"}, "POST")
        self.request("/v1/refs/main", {"target": merged["id"], "expected": first["id"]}, "POST")
        self.assertEqual(list(self.request("/v1/manifest/main?prefix=src")["files"]), ["src/a"])
        self.assertEqual(len(self.request(f'/v1/diff?base={first["id"]}&target=main')["changes"]), 2)
        self.assertEqual(len(self.request("/v1/log/main?limit=2")["changesets"]), 2)
        lines = self.request("/v1/blame/main?path=src/a")["lines"]
        self.assertEqual([line["changeset"] for line in lines], [first["id"], left["id"]])
        self.assert_http_error(409, "/v1/refs/main", {"target": right["id"], "expected": merged["id"]}, "POST")
        self.assert_http_error(409, "/v1/refs/main", {"target": merged["id"], "expected": first["id"]}, "POST")

    def test_invalid_json_and_schema_and_paths(self):
        self.assert_http_error(400, "/v1/changesets", b"[1,2]", "POST")
        self.assert_http_error(400, "/v1/changesets", b"{", "POST")
        self.assert_http_error(400, "/v1/changesets", {"parents": []}, "POST")
        self.assert_http_error(415, "/v1/changesets", b"{}", "POST", headers={"Content-Type": "text/plain"})
        self.assert_http_error(400, "/v1/blobs/../../etc/passwd")
        self.assert_http_error(404, "/v1/nope")
        first = self.commit([], {"x": b"x"})
        self.assert_http_error(400, f'/v1/log/{first["id"]}?limit=-1')
        self.assert_http_error(400, f'/v1/log/{first["id"]}?limit=abc')
        self.assert_http_error(400, "/v1/changesets", {"parents": [], "changes": {"../x": None}, "author": "a", "message": "m"}, "POST")

    def test_shared_cache_and_origin_failure(self):
        first = self.commit([], {"src/a": b"cached contents"})
        oid = first["changes"]["src/a"]["oid"]
        cache = CacheServer(("127.0.0.1", 0), self.url, Path(self.temp.name) / "cache", token="test-token")
        cache_url = self.start(cache)
        self.assert_http_error(401, f"/v1/blobs/{oid}", url=cache_url, token=None)
        self.assertEqual(self.request(f"/v1/blobs/{oid}", url=cache_url), b"cached contents")
        self.assertEqual(self.request(f"/v1/blobs/{oid}", url=cache_url), b"cached contents")
        self.assertEqual(cache.cache.hits, 1)
        self.assertEqual(self.request(f'/v1/trees/{first["root"]}', url=cache_url), {"entries": self.store.get_tree(first["root"])})
        self.request("/v1/refs/main", {"target": first["id"], "expected": None}, "POST", url=cache_url)
        self.assertEqual(self.request("/v1/refs", url=cache_url)["refs"]["main"], first["id"])
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self.request(f"/v1/blobs/{oid}", url=cache_url), b"cached contents")
        self.assert_http_error(502, "/v1/refs", url=cache_url)

    def test_cache_outage_circuit_preserves_cached_objects(self):
        oid = self.store.put_blob(b"available during outage")
        cache = CacheServer(("127.0.0.1", 0), self.url, Path(self.temp.name) / "guarded-cache", token="test-token")
        url = self.start(cache)
        self.request(f"/v1/blobs/{oid}", url=url)
        self.server.shutdown()
        self.server.server_close()
        for _ in range(3):
            self.assert_http_error(502, "/v1/refs", url=url)
        self.assert_http_error(503, "/v1/refs", url=url)
        self.assertEqual(self.request(f"/v1/blobs/{oid}", url=url), b"available during outage")
        stats = self.request("/v1/cache/stats", url=url)
        self.assertTrue(stats["circuit_open"])
        self.assertEqual(stats["upstream_requests"], 4)
        self.assertEqual(stats["upstream_rejections"], 1)
        self.assert_http_error(401, "/v1/cache/stats", url=url, token=None)

    def test_identical_protocol_across_two_cache_tiers(self):
        first = self.commit([], {"src/x": b"through both tiers"})
        oid = first["changes"]["src/x"]["oid"]
        regional = CacheServer(("127.0.0.1", 0), self.url, Path(self.temp.name) / "regional", token="test-token")
        regional_url = self.start(regional)
        node = CacheServer(("127.0.0.1", 0), regional_url, Path(self.temp.name) / "node", token="test-token")
        node_url = self.start(node)
        self.assertEqual(self.request(f"/v1/blobs/{oid}", url=node_url), b"through both tiers")
        self.assertEqual(node.guard.requests, 1)
        self.assertEqual(regional.guard.requests, 1)
        self.assertEqual(self.request(f'/v1/trees/{first["root"]}', url=node_url)["entries"], self.store.get_tree(first["root"]))
        self.request("/v1/refs/main", {"target": first["id"], "expected": None}, "POST", url=node_url)
        self.assertEqual(self.store.resolve("main"), first["id"])

    def test_server_concurrency_limit_rejects_without_spawning(self):
        server = Server(("127.0.0.1", 0), self.store, "test-token", max_workers=1)
        url = self.start(server)
        server.workers.acquire()
        try:
            self.assert_http_error(503, "/health", url=url)
        finally:
            server.workers.release()
        self.assertEqual(self.request("/health", url=url)["status"], "ok")


class CacheTests(unittest.TestCase):
    def test_upstream_guard_limits_outage_and_single_recovery_probe(self):
        now = [100.0]
        guard = UpstreamGuard(limit=2, failure_threshold=2, cooldown=5, clock=lambda: now[0])
        first, second = guard.enter(), guard.enter()
        with self.assertRaises(SCNError) as caught:
            guard.enter()
        self.assertEqual(caught.exception.status, 503)
        guard.finish(first, healthy=False)
        guard.finish(second, healthy=False)
        self.assertTrue(guard.stats()["circuit_open"])
        with self.assertRaises(SCNError):
            guard.enter()
        now[0] += 5
        probe = guard.enter()
        self.assertTrue(probe)
        with self.assertRaises(SCNError):
            guard.enter()
        guard.finish(probe, healthy=False)
        now[0] += 5
        probe = guard.enter()
        guard.finish(probe, healthy=True)
        self.assertFalse(guard.stats()["circuit_open"])
        ordinary = guard.enter()
        self.assertFalse(ordinary)
        guard.finish(ordinary, healthy=True)
        self.assertEqual(guard.stats()["upstream_inflight"], 0)

    def test_integrity_corruption_and_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = ObjectCache(temp, 100)
            data = bytes(range(100))
            oid = hashlib.sha256(b"blob\0" + data).hexdigest()
            cache.put("blobs", oid, data)
            self.assertLessEqual(sum(p.stat().st_size for p in Path(temp).glob("*.z")), 100)
            small = b"hello"
            oid = hashlib.sha256(b"blob\0" + small).hexdigest()
            cache.put("blobs", oid, small)
            self.assertEqual(cache.get("blobs", oid), small)
            (Path(temp) / f"blobs-{oid}.z").write_bytes(b"broken")
            self.assertIsNone(cache.get("blobs", oid))
            with self.assertRaises(SCNError):
                cache.put("blobs", oid, b"wrong")


if __name__ == "__main__":
    unittest.main()

"""Independent regression checks for cache failure isolation and workspace races."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from openscn.cache import CacheServer
from openscn.client import Workspace
from openscn.errors import SCNError
from openscn.server import Server
from openscn.store import Store


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / "store")
        self.url = self.start(Server(("127.0.0.1", 0), self.store))
        environment = patch.dict(os.environ, {"OPENSCN_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def changeset(self, parents=(), **files):
        changes = {name: None if content is None else {
            "oid": self.store.put_blob(content), "executable": False
        } for name, content in files.items()}
        return self.store.create_changeset(list(parents), changes, "review", "tester")

    @staticmethod
    def read(url, oid):
        with urlopen(Request(url + "/v1/blobs/" + oid), timeout=10) as response:
            return response.read()

    def cache(self, max_bytes=256 * 1024 * 1024):
        server = CacheServer(("127.0.0.1", 0), self.url, self.root / "cache", max_bytes=max_bytes)
        return server, self.start(server)

    def test_cache_disk_read_failure_does_not_hide_healthy_origin(self):
        content = b"available from origin"
        oid = self.store.put_blob(content)
        cache, url = self.cache()
        with patch.object(cache.cache, "get", side_effect=OSError("unavailable cache disk")):
            self.assertEqual(self.read(url, oid), content)

    def test_cache_disk_write_failure_does_not_hide_healthy_origin(self):
        content = b"available from origin"
        oid = self.store.put_blob(content)
        cache, url = self.cache()
        with patch.object(cache.cache, "put", side_effect=OSError("full cache disk")):
            self.assertEqual(self.read(url, oid), content)

    def test_concurrent_cold_cache_requests_share_one_origin_read(self):
        self.check_concurrent_object_reads()

    def test_concurrent_reads_coalesce_when_disk_cache_is_disabled(self):
        self.check_concurrent_object_reads(max_bytes=0)

    def check_concurrent_object_reads(self, max_bytes=256 * 1024 * 1024):
        content = b"one immutable object"
        oid = self.store.put_blob(content)
        cache, url = self.cache(max_bytes)
        barrier = threading.Barrier(8)
        original = self.store.get_blob
        calls = []
        def delayed_get(requested):
            calls.append(requested)
            time.sleep(0.25)
            return original(requested)
        def fetch(_):
            barrier.wait(timeout=10)
            return self.read(url, oid)
        with patch.object(self.store, "get_blob", side_effect=delayed_get):
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(fetch, range(8))), [content] * 8)
        self.assertEqual(calls, [oid])

    def test_coalesced_failure_releases_waiters_and_allows_retry(self):
        content = b"eventual success"
        oid = self.store.put_blob(content)
        cache, url = self.cache()
        barrier = threading.Barrier(8)
        calls = []
        def fail(*args, **kwargs):
            calls.append(args)
            time.sleep(0.25)
            raise SCNError("injected upstream failure", 502)
        def fetch(_):
            barrier.wait(timeout=10)
            with self.assertRaisesRegex(SCNError, "injected upstream failure"):
                cache.object("blobs", oid)
        with patch.object(cache, "fetch", side_effect=fail):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(fetch, range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.stats()["object_fetches_inflight"], 0)
        self.assertEqual(self.read(url, oid), content)

    def test_corrupt_origin_object_is_rejected_then_retry_succeeds(self):
        content = b"correct content"
        oid = self.store.put_blob(content)
        cache, url = self.cache()
        with patch.object(self.store, "get_blob", return_value=b"corrupt content"):
            with self.assertRaisesRegex(SCNError, "integrity"):
                cache.object("blobs", oid)
        self.assertEqual(self.read(url, oid), content)

    def workspace(self):
        first = self.changeset(tracked=b"original")
        self.store.update_ref("main", first["id"], None)
        return first, Workspace.clone(self.url, self.root / "workspace")

    def test_checkout_preserves_file_edited_while_fetching_before_deletion(self):
        first, workspace = self.workspace()
        next_change = self.changeset([first["id"]], tracked=None, added=b"new")
        original = workspace.client.get_blob
        def edit_then_fetch(oid):
            (workspace.root / "tracked").write_bytes(b"concurrent local edit")
            return original(oid)
        with patch.object(workspace.client, "get_blob", side_effect=edit_then_fetch):
            with self.assertRaises(SCNError):
                workspace.update(next_change["id"])
        self.assertEqual((workspace.root / "tracked").read_bytes(), b"concurrent local edit")
        self.assertFalse((workspace.root / "added").exists())
        self.assertEqual(Workspace.open(workspace.root).metadata["head"], first["id"])

    def test_checkout_preserves_untracked_file_created_while_fetching(self):
        first, workspace = self.workspace()
        next_change = self.changeset([first["id"]], added=b"server bytes")
        original = workspace.client.get_blob
        def create_then_fetch(oid):
            (workspace.root / "added").write_bytes(b"concurrent untracked work")
            return original(oid)
        with patch.object(workspace.client, "get_blob", side_effect=create_then_fetch):
            with self.assertRaises(SCNError):
                workspace.update(next_change["id"])
        self.assertEqual((workspace.root / "added").read_bytes(), b"concurrent untracked work")
        self.assertEqual(Workspace.open(workspace.root).metadata["head"], first["id"])

    def test_recovery_keeps_post_crash_edits_to_completed_operations(self):
        first, workspace = self.workspace()
        next_change = self.changeset([first["id"]], tracked=b"server replacement", zadded=b"new file")
        original = workspace._apply_checkout_operation
        def interrupt_second(operation, staging):
            if operation["path"] == "zadded":
                raise RuntimeError("simulated process interruption")
            return original(operation, staging)
        with patch.object(workspace, "_apply_checkout_operation", side_effect=interrupt_second):
            with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                workspace.update(next_change["id"])
        self.assertEqual((workspace.root / "tracked").read_bytes(), b"server replacement")
        (workspace.root / "tracked").write_bytes(b"edit after interruption")
        reopened = Workspace.open(workspace.root)
        with patch.object(reopened.client, "request", side_effect=AssertionError("recovery must be offline")):
            self.assertEqual(reopened.recover(), next_change["id"])
        self.assertEqual((workspace.root / "tracked").read_bytes(), b"edit after interruption")
        self.assertEqual((workspace.root / "zadded").read_bytes(), b"new file")
        self.assertEqual([(item["path"], item["status"]) for item in reopened.status()], [("tracked", "modified")])

    def test_simultaneous_ref_writers_have_exactly_one_winner(self):
        first = self.changeset(tracked=b"first")
        self.store.update_ref("main", first["id"], None)
        candidates = [self.changeset([first["id"]], tracked=str(i).encode())["id"] for i in range(8)]
        barrier = threading.Barrier(len(candidates))
        def land(target):
            barrier.wait(timeout=10)
            try:
                return self.store.update_ref("main", target, first["id"])["target"]
            except SCNError as exc:
                self.assertEqual(exc.status, 409)
                return None
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            winners = [value for value in pool.map(land, candidates) if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.resolve("main"), winners[0])

    def test_indexed_history_and_blame_handle_directory_deletion_and_side_branches(self):
        first = self.changeset(**{"src/file": b"same line\n"})
        side = self.changeset([first["id"]], **{"src/file": b"side branch\n"})
        deleted = self.changeset([first["id"]], src=None)
        readded = self.changeset([deleted["id"]], **{"src/file": b"same line\n"})
        tip = self.changeset([readded["id"], side["id"]], unrelated=b"unrelated")
        expected = [readded["id"], deleted["id"], first["id"]]
        for clear in (False, True):
            if clear:
                self.store.clear_derived()
            self.assertEqual([change["id"] for change in self.store.log(tip["id"], path="src/file")], expected)
            self.assertEqual([change["id"] for change in self.store.log(tip["id"], path="src")], expected)
            self.assertEqual([line["changeset"] for line in self.store.blame(tip["id"], "src/file")], [readded["id"]])
            self.assertTrue(self.store.is_ancestor(side["id"], tip["id"]))
            self.assertFalse(self.store.is_ancestor(side["id"], readded["id"]))
            self.assertEqual(self.store.first_parent_ancestor(tip["id"], 2), deleted["id"])
            self.assertEqual(self.store.diff(first["id"], readded["id"]), [])


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import hashlib
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openscn.errors import SCNError
from openscn.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def entry(self, text, executable=False):
        return {"oid": self.store.put_blob(text.encode()), "executable": executable}

    def commit(self, parents=None, changes=None, message="change", author="Ada"):
        return self.store.create_changeset(parents or [], changes or {}, message, author)

    def test_objects_and_restart(self):
        entry = self.entry("hello\n")
        self.assertEqual(entry["oid"], hashlib.sha256(b"blob\0hello\n").hexdigest())
        initial = self.commit(changes={"src/a.py": entry, "bin/run": self.entry("run", True)})
        self.store.update_ref("main", initial["id"], None)
        restored = Store(self.temp.name)
        self.assertEqual(restored.get_changeset("main"), initial)
        self.assertEqual(restored.manifest("main", ["src"]), {"src/a.py": entry})
        self.assertEqual(restored.get_blob(entry["oid"]), b"hello\n")
        self.assertEqual(restored.get_tree(initial["root"])["src"]["kind"], "tree")
        with self.assertRaises(SCNError):
            restored.get_blob(initial["root"])

    def test_incremental_tree_update(self):
        entries = {f"dir{i}/nested/file": self.entry(str(i)) for i in range(80)}
        initial = self.commit(changes=entries)
        old_tree = self.store.get_tree(initial["root"])
        reads = []
        original = self.store.get_tree
        def tracked(oid):
            reads.append(oid)
            return original(oid)
        with patch.object(self.store, "get_tree", tracked), patch.object(self.store, "manifest", side_effect=AssertionError("full manifest on commit")):
            next_change = self.commit([initial["id"]], {"dir3/nested/file": self.entry("changed")})
        self.assertEqual(len(reads), 3)
        new_tree = self.store.get_tree(next_change["root"])
        self.assertEqual(old_tree["dir4"], new_tree["dir4"])
        self.assertNotEqual(old_tree["dir3"], new_tree["dir3"])
        self.assertEqual(list(next_change["changes"]), ["dir3/nested/file"])

    def test_file_directory_transitions(self):
        a = self.entry("a")
        b = self.entry("b")
        initial = self.commit(changes={"x": a, "keep": b})
        with self.assertRaises(SCNError):
            self.commit([initial["id"]], {"x/y": b})
        directory = self.commit([initial["id"]], {"x": None, "x/y": b})
        self.assertEqual(self.store.manifest(directory["id"]), {"x/y": b, "keep": b})
        with self.assertRaises(SCNError):
            self.commit([directory["id"]], {"x": a})
        file_again = self.commit([directory["id"]], {"x/y": None, "x": a})
        self.assertEqual(self.store.manifest(file_again["id"]), {"x": a, "keep": b})
        removed = self.commit([file_again["id"]], {"x/y/z": None})
        self.assertEqual(removed["root"], file_again["root"])
        with self.assertRaises(SCNError):
            self.commit(changes={"x": a, "x/y": b})

    def test_cas_only_one_writer_wins(self):
        initial = self.commit()
        self.store.update_ref("main", initial["id"], None)
        children = [self.commit([initial["id"]], {str(i): self.entry(str(i))}) for i in range(12)]
        def update(child):
            try:
                self.store.update_ref("main", child["id"], initial["id"])
                return True
            except SCNError as error:
                self.assertEqual(error.status, 409)
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(update, children))
        self.assertEqual(sum(results), 1)
        self.assertIn(self.store.refs()["main"], [c["id"] for c in children])

    def test_merge_dag_and_history(self):
        initial = self.commit(changes={"a": self.entry("first\nsecond\n")})
        left = self.commit([initial["id"]], {"a": self.entry("first\nchanged\n")}, author="Bea")
        right = self.commit([initial["id"]], {"dir/b": self.entry("branch")})
        merged = self.store.merge(left["id"], right["id"], "merge", "M")
        self.assertEqual(merged["parents"], [left["id"], right["id"]])
        self.assertTrue(self.store.is_ancestor(right["id"], merged["id"]))
        self.assertFalse(self.store.is_ancestor(left["id"], right["id"]))
        self.assertEqual(self.store.merge_base(left["id"], right["id"]), initial["id"])
        self.assertEqual(self.store.merge_base(merged["id"], right["id"]), right["id"])
        self.assertEqual([c["id"] for c in self.store.log(merged["id"], path="a")], [left["id"], initial["id"]])
        self.assertEqual([l["author"] for l in self.store.blame(merged["id"], "a")], ["Ada", "Bea"])
        self.assertEqual([d["path"] for d in self.store.diff(left["id"], merged["id"])], ["dir/b"])
        rebased = self.store.rebase(right["id"], left["id"], "rebase", "M")
        self.assertEqual(rebased["parents"], [left["id"]])
        self.assertEqual(self.store.manifest(rebased["id"]), self.store.manifest(merged["id"]))

    def test_conflicts(self):
        initial = self.commit(changes={"a": self.entry("base")})
        left = self.commit([initial["id"]], {"a": self.entry("left")})
        right = self.commit([initial["id"]], {"a": self.entry("right")})
        for operation in [lambda: self.store.merge(left["id"], right["id"], "m", "a"),
                          lambda: self.store.rebase(right["id"], left["id"], "r", "a")]:
            with self.assertRaises(SCNError) as error:
                operation()
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(error.exception.details, {"conflicts": ["a"]})
        deleted = self.commit([initial["id"]], {"a": None})
        with self.assertRaises(SCNError):
            self.store.merge(deleted["id"], left["id"], "m", "a")
        empty = self.commit()
        file_side = self.commit([empty["id"]], {"x": self.entry("file")})
        dir_side = self.commit([empty["id"]], {"x/y": self.entry("nested")})
        with self.assertRaises(SCNError) as error:
            self.store.merge(file_side["id"], dir_side["id"], "m", "a")
        self.assertEqual(error.exception.details, {"conflicts": ["x", "x/y"]})

    def test_invalid_paths_and_objects(self):
        entry = self.entry("safe")
        for path in ["../escape", "/root", "a//b", "a/./b", ".git/config", ".GiT/config", "a/.openscn/file", "a/.OpenSCN/config", "a\\b", "a\x00b", "", "/".join(["a"] * 130)]:
            with self.subTest(path=path), self.assertRaises(SCNError):
                self.commit(changes={path: entry})
        for oid in ["../outside", "A" * 64, "a" * 63, None]:
            with self.subTest(oid=oid), self.assertRaises(SCNError):
                self.store.get_blob(oid)
        with self.assertRaises(SCNError):
            self.commit(changes={"link": {"kind": "symlink", "oid": entry["oid"]}})
        with self.assertRaises(SCNError):
            self.commit(changes={"bad": {"oid": "0" * 64}})
        with self.assertRaises(SCNError):
            self.commit(changes={"bad": {"oid": entry["oid"], "executable": "false"}})

    def test_corruption_and_symlinks_rejected(self):
        entry = self.entry("safe")
        path = self.store._object_path(entry["oid"])
        path.write_bytes(b"broken")
        with self.assertRaises(SCNError) as error:
            self.store.get_blob(entry["oid"])
        self.assertEqual(error.exception.status, 500)
        path.unlink()
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(b"secret")
        path.symlink_to(outside)
        with self.assertRaises(SCNError):
            self.store.get_blob(entry["oid"])

    def test_randomized_changes_against_flat_model(self):
        rng = random.Random(7391)
        model = {}
        current = self.commit()
        pool = [f"d{i}/sub{j}/f{k}" for i in range(4) for j in range(3) for k in range(3)]
        for step in range(100):
            previous = dict(model)
            changes = {}
            for path in rng.sample(pool, rng.randint(1, 5)):
                if rng.random() < 0.35:
                    changes[path] = None
                    model.pop(path, None)
                else:
                    entry = self.entry(f"value-{rng.randrange(20)}", rng.choice([False, True]))
                    changes[path] = entry
                    model[path] = entry
            next_change = self.commit([current["id"]], changes)
            with self.subTest(step=step):
                self.assertEqual(self.store.manifest(next_change["id"]), model)
                expected = [{"path": p, "before": previous.get(p), "after": model.get(p)}
                            for p in sorted(previous.keys() | model.keys()) if previous.get(p) != model.get(p)]
                self.assertEqual(self.store.diff(current["id"], next_change["id"]), expected)
            current = next_change
        self.assertEqual(Store(self.temp.name).manifest(current["id"]), model)

    def test_randomized_branch_merges_against_flat_model(self):
        rng = random.Random(147)
        base_files = {f"f{i}": self.entry(f"base-{i}") for i in range(20)}
        base = self.commit(changes=base_files)
        for step in range(20):
            left_changes, right_changes = {}, {}
            paths = list(base_files)
            rng.shuffle(paths)
            for target, selected in [(left_changes, paths[:5]), (right_changes, paths[5:10])]:
                for path in selected:
                    target[path] = None if rng.random() < 0.4 else self.entry(f"change-{step}-{path}")
            left = self.commit([base["id"]], left_changes)
            right = self.commit([base["id"]], right_changes)
            expected = dict(base_files)
            for path, value in dict(left_changes, **right_changes).items():
                if value is None:
                    expected.pop(path, None)
                else:
                    expected[path] = value
            with self.subTest(step=step):
                merged = self.store.merge(left["id"], right["id"], "m", "M")
                self.assertEqual(self.store.manifest(merged["id"]), expected)
                rebased = self.store.rebase(right["id"], left["id"], "r", "M")
                self.assertEqual(self.store.manifest(rebased["id"]), expected)


if __name__ == "__main__":
    unittest.main()

"""Reproducible local query measurements, not a fleet-scale throughput claim.

Run after installation: python examples/benchmark.py --files 1000 --commits 200
The fixture and all data are removed afterward. Counts are Store method calls,
not disk I/O counts. Timings include Python and SQLite connection overhead.
"""

import argparse
import json
import platform
import tempfile
import time

from openscn.store import Store


class MeasuredStore(Store):
    def __init__(self, directory):
        self.reads = {"changesets": 0, "trees": 0, "graph_nodes": 0, "blobs": 0}
        super().__init__(directory)

    def get_changeset(self, rev):
        self.reads["changesets"] += 1
        return super().get_changeset(rev)

    def get_tree(self, oid):
        self.reads["trees"] += 1
        return super().get_tree(oid)

    def _graph(self, oid):
        self.reads["graph_nodes"] += 1
        return super()._graph(oid)

    def get_blob(self, oid):
        self.reads["blobs"] += 1
        return super().get_blob(oid)

    def measure(self, operation):
        self.reads = dict.fromkeys(self.reads, 0)
        start = time.perf_counter()
        value = operation()
        return {"seconds": round(time.perf_counter() - start, 6), "reads": dict(self.reads)}, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--commits", type=int, default=200)
    args = parser.parse_args()
    if args.files < 1 or args.commits < 2:
        parser.error("--files must be positive and --commits at least 2")
    with tempfile.TemporaryDirectory(prefix="openscn-benchmark-") as directory:
        store = MeasuredStore(directory)
        oid = store.put_blob(b"original\n")
        entry = {"oid": oid, "executable": False}
        files = {f"packages/package-{i // 10}/file-{i}.txt": entry for i in range(args.files)}
        files["src/target.txt"] = entry
        initial = store.create_changeset([], files, "fixture", "benchmark")
        tip = initial
        for i in range(args.commits - 1):
            entry = {"oid": store.put_blob(f"unrelated {i}\n".encode()), "executable": False}
            tip = store.create_changeset([tip["id"]], {"noise.txt": entry}, "unrelated edit", "benchmark")
        parent = tip
        entry = {"oid": store.put_blob(b"original\nnew line\n"), "executable": False}
        tip = store.create_changeset([tip["id"]], {"src/target.txt": entry}, "target edit", "benchmark")
        metrics = {}
        metrics["one_file_diff"], diff = store.measure(lambda: store.diff(parent["id"], tip["id"]))
        assert [change["path"] for change in diff] == ["src/target.txt"]
        metrics["indexed_path_history"], history = store.measure(lambda: store.log(tip["id"], path="src/target.txt"))
        assert [change["id"] for change in history] == [tip["id"], initial["id"]]
        metrics["first_parent_jump"], ancestor = store.measure(lambda: store.first_parent_ancestor(tip["id"], args.commits))
        assert ancestor == initial["id"]
        metrics["cold_blame"], cold = store.measure(lambda: store.blame(tip["id"], "src/target.txt"))
        reopened = MeasuredStore(directory)
        metrics["persisted_blame_after_restart"], warm = reopened.measure(lambda: reopened.blame(tip["id"], "src/target.txt"))
        assert warm == cold
        print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                          "fixture": {"unrelated_files": args.files, "changesets": args.commits + 1},
                          "measurements": metrics}, indent=2))


if __name__ == "__main__":
    main()

# Local query measurements

Run the reproducible fixture after installing OpenSCN:

```sh
python examples/benchmark.py --files 1000 --commits 200
```

It creates 1,000 unrelated files, one watched file, and a 201-changeset linear
history. Only the first and last changesets touch the watched file. All data is
temporary. JSON output includes wall time and instrumented Store method calls.
The counts below were observed on macOS with Python 3.11.15 during the 0.2 review:

| Query | Changeset payload reads | Tree reads | Graph reads | Blob reads |
| --- | ---: | ---: | ---: | ---: |
| Diff with one changed file | 2 | 4 | 0 | 0 |
| Watched-file history | 2 | 0 | 5 | 0 |
| First-parent ancestor 200 edges away | 0 | 0 | 3 | 0 |
| First watched-file blame | 2 | 4 | 6 | 2 |
| Same blame after restarting Store | 0 | 0 | 0 | 0 |

These are method-call counts, not disk I/O counts. SQLite still performs queries
on warm reads. Timing depends on hardware, directory layout, history, operating
system caches, and Python/SQLite overhead. The fixture does not measure network
throughput, concurrent write capacity, or enterprise-scale repositories.

Unit tests independently check equal-tree pruning, branch-correct indexed path
queries, logarithmic first-parent jumps, cache persistence and rebuild, and
backfill beyond Python's recursion limit. Fault tests cover cache outages and
workspace interruption. None of this establishes Anthropic-scale performance;
the [comparison](scion-comparison.md) lists remaining architectural gaps.

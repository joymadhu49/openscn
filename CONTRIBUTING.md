# Contributing

OpenSCN welcomes small, reviewable improvements with clear behavior and tests.
File a GitHub issue for a bug or proposed feature. Include the version, platform,
minimal reproduction, expected result, and actual result. Remove credentials
and private source content from reports.

Use Python 3.11 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/demo.py
```

Keep runtime dependencies in the standard library. Use `unittest` for tests,
temporary directories for storage, and loopback ephemeral ports for network
tests. Add meaningful regression coverage for changed behavior, especially
path safety, concurrent ref updates, merge semantics, and sparse checkout.
Read [AGENTS.md](AGENTS.md) for the repository's shared interfaces.

Submit a pull request describing the problem, resulting behavior, and checks
performed. Update relevant API and CLI documentation when behavior changes.
Do not include local workspace metadata, server objects, credentials, generated
build outputs, or claims of performance without reproducible measurements.

Contributions are distributed under the project's MIT license. Do not copy
proprietary implementation material into the repository.

## Release checklist

1. Update `openscn/__init__.py`, `pyproject.toml`, the changelog,
   `docs/release-notes.md`, and installation examples for the new version.
2. Run the test suite, API demo, `python -m build`, and
   `python -m twine check dist/*` with development build tools installed.
3. Commit reviewed changes, create a matching `vX.Y.Z` tag, and push it.
4. The release workflow tests, builds, and attaches the wheel, source archive,
   and SHA-256 checksums to a GitHub prerelease. Inspect its successful result.

The workflow does not publish to PyPI or deploy a public server.

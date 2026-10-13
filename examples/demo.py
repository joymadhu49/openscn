"""Run a temporary OpenSCN server and exercise its public HTTP API.

Install OpenSCN first, then run: python examples/demo.py
Only the Python standard library is needed. No persistent data is created.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    def request(method: str, path: str, payload=None):
        headers = {}
        token = os.environ.get("OPENSCN_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if isinstance(payload, bytes):
            data = payload
            headers["Content-Type"] = "application/octet-stream"
        elif payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = None
        req = Request(base_url + path, data=data, headers=headers, method=method)
        with urlopen(req, timeout=5) as response:
            body = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                return json.loads(body)
            return body

    def blob(data: bytes) -> dict:
        return {"oid": request("PUT", "/v1/blobs", data)["oid"], "executable": False}

    def changeset(parents: list[str], changes: dict, message: str) -> dict:
        return request("POST", "/v1/changesets", {
            "parents": parents, "changes": changes, "message": message,
            "author": "OpenSCN demo",
        })

    with tempfile.TemporaryDirectory(prefix="openscn-demo-") as directory:
        with tempfile.TemporaryFile() as server_output:
            server = subprocess.Popen(
                [sys.executable, "-m", "openscn", "serve", "--data", directory,
                 "--host", "127.0.0.1", "--port", str(port)],
                stdout=server_output, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if server.poll() is not None:
                        server_output.seek(0)
                        raise RuntimeError(server_output.read().decode(errors="replace"))
                    try:
                        health = request("GET", "/health")
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("The temporary server did not become ready")
                        time.sleep(0.05)
                print(f"Started OpenSCN {health['version']} at {base_url}")

                base = changeset([], {
                    "src/hello.py": blob(b'print("hello")\n'),
                    "docs/guide.txt": blob(b"An example repository.\n"),
                }, "Create example repository")
                request("POST", "/v1/refs/main", {"target": base["id"], "expected": None})

                left = changeset([base["id"]], {
                    "src/hello.py": blob(b'print("hello, world")\n'),
                }, "Improve greeting")
                right = changeset([base["id"]], {
                    "docs/guide.txt": blob(b"An example with parallel work.\n"),
                }, "Improve guide")
                merged = request("POST", "/v1/merge", {
                    "left": left["id"], "right": right["id"],
                    "message": "Merge independent file changes", "author": "OpenSCN demo",
                })
                request("POST", "/v1/refs/main", {
                    "target": merged["id"], "expected": base["id"],
                })
                assert len(merged["parents"]) == 2
                files = request("GET", "/v1/manifest/main")["files"]
                assert request("GET", "/v1/blobs/" + files["src/hello.py"]["oid"]) == b'print("hello, world")\n'
                assert request("GET", "/v1/blobs/" + files["docs/guide.txt"]["oid"]) == b"An example with parallel work.\n"
                print("Created parallel changesets, merged them, and landed main")

                sparse = request("GET", "/v1/manifest/main?prefix=src")["files"]
                assert set(sparse) == {"src/hello.py"}
                print("Sparse manifest returned only src/hello.py")

                try:
                    request("POST", "/v1/refs/main", {
                        "target": left["id"], "expected": base["id"],
                    })
                except HTTPError as error:
                    with error:
                        assert error.code == 409, error.read().decode(errors="replace")
                    print("Rejected a stale ref update with HTTP 409")
                else:
                    raise AssertionError("The server accepted a stale reference update")

                history = request("GET", "/v1/log/main")["changesets"]
                assert history[0]["id"] == merged["id"]
                lines = request("GET", "/v1/blame/main?path=src/hello.py")["lines"]
                assert len(lines) == 1
                print(f"Verified history and blame; main is {merged['id'][:12]}")
                print("Demo passed. Temporary server and data will be removed.")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    main()

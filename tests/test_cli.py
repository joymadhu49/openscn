"""Exercise the installed CLI boundary with two concurrent developer workflows."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from openscn.server import Server
from openscn.store import Store


class CLITests(unittest.TestCase):
    def test_sparse_commit_concurrent_land_and_rebase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            server = Server(("127.0.0.1", 0), Store(root / "server"), "integration-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = dict(os.environ, OPENSCN_TOKEN="integration-token", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            url = f"http://127.0.0.1:{server.server_port}"

            def cli(*args, cwd=root, expected=0):
                result = subprocess.run([sys.executable, "-m", "openscn", *args], cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout + result.stderr

            try:
                cli("clone", url, "alice")
                alice = root / "alice"
                (alice / "src").mkdir()
                (alice / "docs").mkdir()
                (alice / "src/main.py").write_text("print('hello')\n")
                (alice / "docs/readme").write_text("first\n")
                cli("commit", "-m", "initial", cwd=alice)
                cli("land", cwd=alice)
                cli("clone", url, "bob", "--sparse", "src")
                cli("clone", url, "carol")
                bob, carol = root / "bob", root / "carol"
                self.assertFalse((bob / "docs").exists())
                (bob / "src/main.py").write_text("print('from bob')\n")
                cli("commit", "-m", "bob's change", cwd=bob)
                cli("land", cwd=bob)
                (carol / "docs/readme").write_text("from carol\n")
                cli("commit", "-m", "carol's change", cwd=carol)
                self.assertIn("Reference changed", cli("land", cwd=carol, expected=1))
                cli("rebase", "main", "-m", "replay carol", cwd=carol)
                cli("land", cwd=carol)
                cli("update", cwd=alice)
                self.assertEqual((alice / "src/main.py").read_text(), "print('from bob')\n")
                self.assertEqual((alice / "docs/readme").read_text(), "from carol\n")
                self.assertIn("Working copy clean", cli("status", cwd=alice))
                self.assertIn("bob's change", cli("log", cwd=alice))
                self.assertIn("from bob", cli("blame", "src/main.py", cwd=alice))
                (alice / "src/main.py").write_text("my local edit\n")
                self.assertIn("local changes", cli("update", cwd=alice, expected=1))
                self.assertEqual((alice / "src/main.py").read_text(), "my local edit\n")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from restore_drill import recover

CLI = Path(__file__).with_name("restore_drill.py")


class DatabaseDrill(unittest.TestCase):
    def test_database_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = {"healthy": (0, "ok"), "wrong_schema": (1, "required_table_missing"),
                        "empty_table": (1, "insufficient_rows"), "garbage": (1, "not_sqlite"),
                        "absent": (1, "artifact_missing_or_too_large")}
            for fixture, (status, reason) in expected.items():
                with self.subTest(fixture=fixture):
                    path = Path(tmp) / (fixture + ".db")
                    if fixture == "garbage":
                        path.write_bytes(b"not a database")
                    elif fixture != "absent":
                        with sqlite3.connect(path) as db:
                            db.execute("CREATE TABLE users(id)" if fixture != "wrong_schema"
                                       else "CREATE TABLE orders(id)")
                            if fixture == "healthy":
                                db.execute("INSERT INTO users VALUES (1)")
                    before = path.read_bytes() if path.exists() else None
                    p = subprocess.run([sys.executable, str(CLI), "file", str(path), "--table", "users"],
                                       capture_output=True, text=True, check=False)
                    self.assertEqual(p.returncode, status, p.stderr)
                    self.assertEqual(json.loads(p.stdout), {"engine": "file", "passed": status == 0,
                                                           "reason": reason})
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_bounded_recovery_transport(self):
        cases = [
            ("print('ok')", "ok"),
            ("print('x' * 1000000)", "artifact_too_large"),
            ("import time; time.sleep(5)", "restore_timed_out"),
            ("raise SystemExit(3)", "restore_failed"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for n, (script, expected) in enumerate(cases):
                with self.subTest(expected=expected):
                    out = Path(tmp) / (str(n) + '.db')
                    self.assertEqual(recover([sys.executable, "-c", script], out,
                                             limit=100, timeout=0.4), expected)
                    self.assertLessEqual(out.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()

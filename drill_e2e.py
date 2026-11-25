"""Acceptance test against installed command-line backup engines."""
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile

CLI = Path(__file__).with_name("restore_drill.py").resolve()


def run(args, env, expected=0):
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != expected:
        raise RuntimeError(f"{Path(args[0]).name}: expected exit {expected}, got {proc.returncode}: {proc.stderr}")
    return proc.stdout.strip()


def main():
    missing = [name for name in ("restic", "kopia") if shutil.which(name) is None]
    if missing:
        print("BLOCKED: install the official CLI tools to run this test: " + ", ".join(missing),
              file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="restore-drill-e2e-") as work:
        root = Path(work)
        repo, config, k_repo = root / "restic", root / "kopia.config", root / "kopia"
        password = secrets.token_hex(24)
        env = dict(os.environ, RESTIC_PASSWORD=password, KOPIA_PASSWORD=password,
                   HOME=work, XDG_CACHE_HOME=str(root / "cache"), XDG_CONFIG_HOME=str(root / "config"))
        restic = ["restic", "--repo", str(repo)]
        kopia = ["kopia", "--config-file", str(config), "--no-progress"]
        run(restic + ["init"], env)
        run(kopia + ["repository", "create", "filesystem", "--path", str(k_repo), "--no-check-for-updates"], env)
        for healthy in (True, False):
            folder = root / ("healthy" if healthy else "wrong-schema")
            folder.mkdir()
            database = folder / "fixture.sqlite"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE users(id)" if healthy else "CREATE TABLE orders(id)")
                if healthy:
                    db.execute("INSERT INTO users VALUES (1)")
            result = run(restic + ["backup", "--json", str(database)], env)
            rid = next(row["snapshot_id"] for row in map(json.loads, result.splitlines())
                       if row.get("message_type") == "summary")
            run(kopia + ["snapshot", "create", str(folder)], env)
            manifest = json.loads(run(kopia + ["snapshot", "list", "--json", str(folder)], env))
            kid = manifest[0]["rootEntry"]["obj"]
            # Source is no longer available during either recovery.
            database.unlink()
            cases = {
                "restic": ["restic", "--repo", str(repo), "--snapshot", rid,
                           "--snapshot-path", str(database)],
                "kopia": ["kopia", "--config", str(config), "--object-path", kid + "/fixture.sqlite"],
            }
            for engine, flags in cases.items():
                raw = run([sys.executable, str(CLI), *flags, "--table", "users"], env,
                          expected=0 if healthy else 1)
                expected = {"engine": engine, "passed": healthy,
                            "reason": "ok" if healthy else "required_table_missing"}
                if json.loads(raw) != expected:
                    raise AssertionError(f"{engine}: unexpected report, {raw}")
                print(json.dumps(dict(fixture=folder.name, **expected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

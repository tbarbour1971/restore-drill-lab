"""Run an isolated PostgreSQL backup verification demo (Python 3.11+, PostgreSQL 17 tested).

    python postgres_drill.py [--bin-dir "/path/to/PostgreSQL/bin"]

Requires the full PostgreSQL server and client tools. Run as a regular,
non-root user. No existing server, credentials or data are used. On success,
prints JSON and exits 0: all three restores ran and the verifier identified
only the complete backup as matching the source. A failed assertion or
PostgreSQL command exits nonzero. The synthetic data, dumps and cluster are
temporary. Timings are local demo measurements, not production RTO/RPO.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time


SETUP = """
SET TIME ZONE 'UTC';
CREATE TABLE settings (currency text NOT NULL);
INSERT INTO settings VALUES ('USD');
CREATE TABLE orders (
    id integer PRIMARY KEY,
    placed_at timestamptz NOT NULL,
    cents integer NOT NULL CHECK (cents > 0)
);
"""
INSERT = """
INSERT INTO orders
SELECT n, TIMESTAMPTZ '2026-01-01 12:00:00+00' - (120-n)*interval '1 minute',
       n*100
FROM generate_series({first}, {last}) AS n;
"""
POINT = "TIMESTAMPTZ '2026-01-01 12:00:00+00'"
CHECK = f"""
SELECT count(*),
       extract(epoch FROM ({POINT} - max(placed_at)))::integer,
       coalesce(sum(cents),0),
       (SELECT count(*) FROM settings WHERE currency='USD')
FROM orders;
"""
ROWS = """
SELECT id, extract(epoch FROM placed_at)::bigint, cents
FROM orders ORDER BY id;
"""
TOOLS = ("initdb", "postgres", "pg_ctl", "psql", "createdb", "pg_dump", "pg_restore")


def drill(bin_dir=None):
    paths = {}
    for name in TOOLS:
        if bin_dir:
            candidate = Path(bin_dir).resolve() / (name + (".exe" if os.name == "nt" else ""))
            path = candidate if candidate.is_file() else None
        else:
            path = shutil.which(name)
        if not path:
            raise RuntimeError(f"Missing {name}. Install PostgreSQL or pass --bin-dir.")
        paths[name] = str(Path(path).resolve())
    # No inherited PostgreSQL service, password file, options, or remote target.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    password = secrets.token_urlsafe(32)
    env.update(PGPASSWORD=password, PGCONNECT_TIMEOUT="2")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = str(sock.getsockname()[1])
    conn = ["-h", "127.0.0.1", "-p", port, "-U", "restore_drill"]
    hidden = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(name, *args, check=True, timeout=30):
        result = subprocess.run(
            [paths[name], *map(str, args)], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=hidden,
        )
        if check and result.returncode:
            raise RuntimeError(f"{name} exited {result.returncode}: {result.stderr.strip()}")
        return result

    def sql(db, statement, check=True):
        return run("psql", *conn, "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                   "-d", db, "-c", statement, check=check)

    def snapshot(db):
        count, gap, cents, settings = sql(db, CHECK).stdout.strip().split("|")
        rows = sql(db, ROWS).stdout.splitlines()
        digest = hashlib.sha256("\n".join(rows).encode("ascii")).hexdigest()
        return (int(count), int(gap) if gap else None, int(cents), int(settings), digest)

    with tempfile.TemporaryDirectory(prefix="restore-drill-pg-") as tmp:
        root = Path(tmp)
        data = root / "cluster"
        pwfile = root / "initial-password"
        descriptor = os.open(pwfile, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as f:
            f.write(password + "\n")
        run("initdb", "-D", data, "-U", "restore_drill", "--auth=scram-sha-256",
            f"--pwfile={pwfile}", "--no-locale", "--encoding=UTF8", timeout=60)
        pwfile.unlink()
        log = root / "server.log"
        with log.open("wb") as logfile:
            args = [paths["postgres"], "-D", str(data), "-h", "127.0.0.1", "-p", port]
            if os.name != "nt":
                args.extend(["-c", "unix_socket_directories="])
            server = subprocess.Popen(args, env=env, stdout=logfile,
                                      stderr=subprocess.STDOUT, creationflags=hidden)
            try:
                deadline = time.monotonic() + 20
                while server.poll() is None and time.monotonic() < deadline:
                    ready = sql("postgres", "SHOW data_directory", check=False)
                    if ready.returncode == 0 and Path(ready.stdout.strip()).resolve() == data.resolve():
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Temporary cluster didn't become ready: " +
                                       log.read_text(encoding="utf-8", errors="replace"))

                run("createdb", *conn, "drill_source")
                sql("drill_source", SETUP + INSERT.format(first=1, last=105))

                def dump(case, *options):
                    archive = root / (case + ".dump")
                    run("pg_dump", *conn, "-Fc", *options, "-f", archive, "drill_source")
                    return archive

                stale = dump("stale")
                sql("drill_source", INSERT.format(first=106, last=120))
                complete = dump("complete")
                incomplete = dump("incomplete", "--exclude-table-data=public.orders")
                source = snapshot("drill_source")
                if source[:4] != (120, 0, 726000, 1):
                    raise RuntimeError(f"Unexpected source fixture: {source[:4]}")

                results = []
                for name, archive, expected in (
                    ("complete", complete, (120, 0, 726000, 1)),
                    ("stale", stale, (105, 900, 556500, 1)),
                    ("incomplete", incomplete, (0, None, 0, 1)),
                ):
                    toc = run("pg_restore", "--list", archive).stdout
                    if "TABLE public orders" not in toc or "TABLE public settings" not in toc:
                        raise RuntimeError(f"{name}: archive missing required table definitions")
                    db = "drill_" + name
                    start = time.perf_counter()
                    run("createdb", *conn, db)
                    run("pg_restore", *conn, "--single-transaction", "--exit-on-error",
                        "-d", db, archive)
                    recovered = snapshot(db)
                    elapsed = time.perf_counter() - start
                    matches = recovered == source
                    if recovered[:4] != expected or matches != (name == "complete"):
                        raise RuntimeError(f"{name}: unexpected recovered contents {recovered[:4]}")
                    results.append({
                        "backup": name, "archive_readable": True, "restore_exit": 0,
                        "verify_seconds": round(elapsed, 3), "orders": recovered[0],
                        "missing_orders": source[0] - recovered[0],
                        "fixture_latest_record_gap_seconds": recovered[1],
                        "usd_settings_rows": recovered[3], "matches_source": matches,
                    })
                return results
            finally:
                if server.poll() is None:
                    try:
                        run("pg_ctl", "-D", data, "-m", "fast", "-w", "-t", "5",
                            "stop", check=False, timeout=8)
                    finally:
                        if server.poll() is None:
                            try:
                                server.wait(timeout=4)
                            except subprocess.TimeoutExpired:
                                server.kill()
                                server.wait(timeout=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bin-dir", help="PostgreSQL bin directory (default: search PATH)")
    print(json.dumps(drill(parser.parse_args().bin_dir), indent=2))

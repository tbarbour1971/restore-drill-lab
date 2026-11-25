"""Verify an explicitly selected SQLite artifact in an isolated restore."""
import argparse
from contextlib import closing
import json
import queue
import re
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from threading import Event, Thread
import time

MAX_DB_BYTES = 64 * 1024 * 1024


def check_database(path, table, minimum_rows):
    if not path.is_file() or path.stat().st_size > MAX_DB_BYTES:
        return "artifact_missing_or_too_large"
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\0":
            return "not_sqlite"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return "integrity_failed"
            kind = db.execute(
                "SELECT type FROM pragma_table_list WHERE schema='main' AND name=?", (table,)
            ).fetchone()
            if kind != ("table",):
                return "required_table_missing"
            quoted = '"' + table.replace('"', '""') + '"'
            count = db.execute(f"SELECT count(*) FROM main.{quoted}").fetchone()[0]
            return "ok" if count >= minimum_rows else "insufficient_rows"
    except sqlite3.Error:
        return "sqlite_check_failed"


def recover(command, target, limit=MAX_DB_BYTES, timeout=60):
    """Copy stdout with a hard length cap and wall-clock deadline."""
    chunks = queue.Queue(maxsize=2)
    done = Event()

    def deliver(item):
        while not done.is_set():
            try:
                chunks.put(item, timeout=0.05)
                return
            except queue.Full:
                pass

    def read(stream):
        try:
            while not done.is_set():
                data = stream.read1(65536)
                if not data:
                    deliver(None)
                    return
                deliver(data)
        except OSError:
            deliver(False)

    end = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL) as child:
        reader = Thread(target=read, args=(child.stdout,), daemon=True)
        reader.start()
        try:
            with target.open("xb") as output:
                size = 0
                while True:
                    data = chunks.get(timeout=max(0, end - time.monotonic()))
                    if data is None:
                        break
                    if data is False:
                        return "restore_failed"
                    size += len(data)
                    if size > limit:
                        return "artifact_too_large"
                    output.write(data)
            return "ok" if child.wait(timeout=max(0, end - time.monotonic())) == 0 else "restore_failed"
        except (queue.Empty, subprocess.TimeoutExpired):
            return "restore_timed_out"
        finally:
            done.set()
            if child.poll() is None:
                child.kill()
            child.wait()
            reader.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="engine", required=True)
    file = sub.add_parser("file", help="inspect a previously restored standalone SQLite file")
    file.add_argument("file", type=Path)
    restic = sub.add_parser("restic", help="restore a SQLite file from an explicit restic snapshot")
    restic.add_argument("--repo", required=True, type=Path)
    restic.add_argument("--snapshot", required=True)
    restic.add_argument("--snapshot-path", required=True)
    kopia = sub.add_parser("kopia", help="recover a SQLite file by explicit Kopia object path")
    kopia.add_argument("--config", required=True, type=Path)
    kopia.add_argument("--object-path", required=True)
    for mode in (file, restic, kopia):
        mode.add_argument("--table", required=True)
        mode.add_argument("--min-rows", type=int, default=1)
    args = parser.parse_args()
    if args.min_rows < 0:
        parser.error("--min-rows must be nonnegative")
    try:
        if args.engine == "file":
            reason = check_database(args.file, args.table, args.min_rows)
        else:
            if args.engine == "restic":
                if not re.fullmatch(r"[0-9a-f]{8,64}", args.snapshot):
                    parser.error("--snapshot must be an explicit hexadecimal snapshot ID")
                if not args.snapshot_path.startswith("/") or ".." in args.snapshot_path.split("/"):
                    parser.error("--snapshot-path must be an absolute path within the snapshot")
                if not args.repo.is_dir():
                    parser.error("--repo must be a local repository directory")
                command = ["restic", "--repo", str(args.repo.resolve()), "--no-lock", "dump",
                           args.snapshot, args.snapshot_path]
            else:
                root, _, tail = args.object_path.partition("/")
                if not re.fullmatch(r"[a-z]*[0-9a-f]{32,64}", root) or ".." in tail.split("/"):
                    parser.error("--object-path must start with a Kopia object ID")
                if not args.config.is_file():
                    parser.error("--config must point to an existing Kopia configuration")
                command = ["kopia", "--config-file", str(args.config.resolve()),
                           "--no-progress", "show", args.object_path]
            with tempfile.TemporaryDirectory(prefix="restore-drill-") as folder:
                restored = Path(folder) / "artifact.db"
                reason = recover(command, restored)
                if reason == "ok":
                    reason = check_database(restored, args.table, args.min_rows)
    except FileNotFoundError:
        reason = "tool_or_file_not_found"
    except subprocess.TimeoutExpired:
        reason = "restore_timed_out"
    except OSError:
        reason = "io_error"
    print(json.dumps({"engine": args.engine, "passed": reason == "ok", "reason": reason}))
    return 0 if reason == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

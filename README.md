# Restore Drill Lab

Test whether a restored SQLite backup is usable, not merely readable. A
structurally valid database can still be missing the table your app needs.

This is an early prototype focused on a repeatable pass/fail result with
synthetic fixtures. It is not a replacement for full disaster-recovery testing.

## Reproduce the verified result

Requires Python 3.11 or later, with SQLite 3.37 or later. No dependencies
to install for the tests.

    python -m unittest -v

The test invokes the actual CLI on five fixtures: a healthy database, a valid
database with the wrong schema, a valid database with an empty required
table, a non-database file, and a missing file. It checks exit status, output,
and that the input did not change. All fixtures are generated from scratch.

To use a standalone **copy** of a recovered database:

    python restore_drill.py file recovered.sqlite --table users --min-rows 1

A good check exits zero and prints a JSON result. A failed check exits nonzero.
No row contents are printed. Use self-contained database copies; this prototype
does not replay a WAL or support encrypted databases or SQLite extensions.
Use a separate OS sandbox if inputs may be hostile.

## End-to-end acceptance

With the official Restic and Kopia CLI binaries on PATH:

    python3 drill_e2e.py

The harness creates two new temporary repositories and intentionally deletes only
its generated source files before the restores. Each engine must pass the healthy
fixture and reject a valid SQLite database with the wrong schema. No existing
repository is used by the harness. The GitHub Actions workflow downloads
checksum-pinned Linux amd64 binaries to runner scratch and runs the same command.

The standalone SQLite checks and subprocess byte/time bounds passed locally.
[Verified GitHub Actions run](https://github.com/tbarbour1971/restore-drill-lab/actions/runs/45646877629): Ubuntu 24.04, Restic 0.20.0, Kopia 0.23.1.

| Restored fixture | Restic | Kopia |
| --- | --- | --- |
| Required table present with a row | Exit 0 | Exit 0 |
| Valid SQLite file missing the required table | Exit 1 (`required_table_missing`) | Exit 1 (`required_table_missing`) |

This tests local, synthetic repositories only. It does not establish production
readiness, validate remote backends, or prove the rest of a snapshot is usable.

The CLI only reads explicitly selected artifacts, caps temporary output at 64 MiB,
and stops the recovery subprocess after 60 seconds. The byte cap is not a
network-traffic guarantee. Use a closed, self-contained SQLite file; the drill
does not capture a live database or replay its WAL.

## PostgreSQL restore drill

A readable archive and a successful restore do not prove that the right data
came back. Run the three-case demo with Python 3.11+ and the PostgreSQL server
and client tools on PATH (verified on PostgreSQL 17.10 on Windows):

    python postgres_drill.py

If PostgreSQL is not on PATH, pass `--bin-dir` with its bin directory. Run as an
ordinary user, not root. The demo creates a password-protected temporary cluster
on loopback and does not use an existing PostgreSQL service or database.

| Synthetic backup | Restore exit | Orders recovered | Latest order vs. source checkpoint | Matches source |
| --- | --- | ---: | --- | --- |
| Complete | 0 | 120 of 120 | No gap | Yes |
| Stale | 0 | 105 of 120 | 15 minutes behind | No |
| Incomplete | 0 | 0 of 120 | No orders to measure | No |

All three archives were readable; even the incomplete one recovered both table
definitions and the settings row. The check compares counts, order timestamps,
totals, and a digest of the ordered data. The demo exits zero when it observes
all three expected outcomes, not because every backup passed verification.
Timings include creating the restore database, restoring it, and checking its
contents. They are not production recovery estimates. This demo uses PostgreSQL's
native logical dump and restore, not the SQLite Restic/Kopia workflow above.

## Prior art

- [restic-verify](https://github.com/Mariomarquezt/restic-verify): Restic restore and application checks.
- [sqlite-backup-probe](https://github.com/KanadeK/sqlite-backup-probe): standalone SQLite checks and application expectations.
- [Kopia](https://github.com/kopia/kopia): snapshot storage, verification and restore.

The proposed contribution is a consistent, no-server test and evidence format
across backup engines, not a claim to have invented restore testing.

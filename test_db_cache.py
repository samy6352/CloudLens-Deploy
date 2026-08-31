"""The warehouse runs on local disk and publishes to durable storage.

Why this exists at all: on App Service the database is configured onto /home, which is an
Azure Files SMB share. Measured on the instance, 4KB writes with an fsync after each:

    /tmp        1,555 ops/s
    /home/data     38 ops/s

DuckDB fsyncs on commit, so that 40x lands on every refresh. The deployed app wrote 48 rows/s
where the identical code on a local disk did 2,052 -- the same ratio, which is how we know the
cost is the filesystem rather than the CPU, the row count or Azure.

So the database runs on /tmp and is copied back when a load finishes. These checks exist
because that trade is only acceptable if the copy is honest: a publish that half-writes the
durable file, or a restore that silently prefers a stale copy, would lose a refresh quietly and
show a confident number from last week.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb

import app.warehouse as W

checks = 0
failures: list[str] = []


def check(label: str, ok: bool) -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


section("where the database runs")

check("the App Service share gets a local working copy",
      W._local_cache_path(Path("/home/data/costs.duckdb")) is not None)
check("and the working copy keeps the same file name",
      W._local_cache_path(Path("/home/data/costs.duckdb")).name == "costs.duckdb")
check("an ordinary path is used directly, with no copy",
      W._local_cache_path(Path("/var/lib/app/costs.duckdb")) is None)
check("a developer machine is used directly too",
      W._local_cache_path(Path("C:/repo/data/costs.duckdb")) is None)

# The separator check has to survive being run from Windows, where Path rewrites '/home/...'
# to '\home\...' and a naive str().startswith('/home/') never matches -- which would have made
# every one of these checks pass for the wrong reason.
check("the share is recognised regardless of the host's path separator",
      "as_posix" in open("app/warehouse.py", encoding="utf-8").read())

os.environ["COST_DB_LOCAL"] = "0"
try:
    check("it can be turned off explicitly",
          W._local_cache_path(Path("/home/data/costs.duckdb")) is None)
finally:
    del os.environ["COST_DB_LOCAL"]

section("publishing is atomic")

tmpdir = Path(tempfile.mkdtemp())
durable = tmpdir / "durable" / "costs.duckdb"
working = tmpdir / "working" / "costs.duckdb"
durable.parent.mkdir(parents=True)
working.parent.mkdir(parents=True)


class FakeWarehouse:
    """Just the publish/restore pair, without booting the whole Warehouse."""
    def __init__(self):
        self.durable_path = durable
        self.db_path = working
        self._publish_lock = W.threading.Lock()
        self._db = None

    _database = W.Warehouse._database
    _restore = W.Warehouse._restore
    publish = W.Warehouse.publish
    _db_lock = W.threading.Lock()


wh = FakeWarehouse()
con = duckdb.connect(str(working))
con.execute("CREATE TABLE costs (a VARCHAR)")
con.execute("INSERT INTO costs VALUES ('first')")
con.close()
wh._db = duckdb.connect(str(working))

wh.publish()
check("a publish creates the durable file", durable.exists())
check("no temp file is left behind",
      not (tmpdir / "durable" / "costs.duckdb.publishing").exists())

read = duckdb.connect(str(durable), read_only=True)
check("the durable copy holds the committed rows",
      read.execute("SELECT a FROM costs").fetchall() == [("first",)])
read.close()

wh._db.execute("INSERT INTO costs VALUES ('second')")
wh.publish()
read = duckdb.connect(str(durable), read_only=True)
check("a later publish carries the newer rows",
      sorted(r[0] for r in read.execute("SELECT a FROM costs").fetchall())
      == ["first", "second"])
read.close()

# A copy straight onto the live path leaves a window where the durable file is truncated. If
# the instance dies there, the next boot restores a corrupt database -- worse than restoring
# yesterday's, because it looks like a warehouse and reads like one.
src = open("app/warehouse.py", encoding="utf-8").read()
check("the copy lands on a temp name first", '.publishing' in src)
check("and is moved into place with an atomic rename", "os.replace(tmp, self.durable_path)" in src)
# A plain file copy of a live database is not a snapshot: it reads pages while the writer may
# still be moving them. DuckDB's own COPY FROM DATABASE takes a consistent one -- and it is
# also the only version that works while the source file is open, which a file copy is not on
# every platform.
check("the snapshot is taken by the database, not by copying its file",
      "COPY FROM DATABASE" in src and "shutil.copyfile" not in src)
check("a stale temp file from an interrupted publish is cleared first",
      "tmp.unlink(missing_ok=True)" in src)

section("publishing never breaks a refresh")

wh_broken = FakeWarehouse()
wh_broken.durable_path = Path("/nonexistent-directory-xyz/costs.duckdb")
wh_broken._db = wh._db
try:
    wh_broken.publish()
    check("a failed publish is swallowed, not raised", True)
except Exception:
    check("a failed publish is swallowed, not raised", False)

check("the same file needs no publish at all",
      FakeWarehouse.publish.__doc__ is not None
      and "if self.db_path == self.durable_path" in src)

section("restoring prefers whichever copy is newer")

# A redeploy restarts the process but keeps /tmp, so the working copy is usually already
# current. Copying anyway would add startup latency for nothing.
working.unlink()
wh2 = FakeWarehouse()
wh2._restore()
check("a missing working copy is restored from durable", working.exists())
# Close the writer first: DuckDB refuses a second connection to the same file with a different
# configuration, which is exactly why connect() never passes read_only through.
wh._db.close()
read = duckdb.connect(str(working), read_only=True)
check("and it carries the durable rows",
      sorted(r[0] for r in read.execute("SELECT a FROM costs").fetchall())
      == ["first", "second"])
read.close()

time.sleep(0.01)
Path(working).touch()
before = working.stat().st_mtime
wh2._restore()
check("a working copy newer than durable is left alone",
      working.stat().st_mtime == before)

os.remove(durable)
wh3 = FakeWarehouse()
try:
    wh3._restore()
    check("a missing durable file is not an error on first boot", True)
except Exception:
    check("a missing durable file is not an error on first boot", False)

shutil.rmtree(tmpdir, ignore_errors=True)

section("every route that writes also publishes")

exports_src = open("app/exports.py", encoding="utf-8").read()
check("the quick path publishes when it finishes",
      src.count("await self.publish_async()") == 2)
check("the report path publishes too", "await self.publish_async()" in src)
check("the FOCUS/export path publishes as well",
      "await warehouse.publish_async()" in exports_src)
check("the copy runs off the event loop",
      "asyncio.to_thread(self.publish)" in src)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

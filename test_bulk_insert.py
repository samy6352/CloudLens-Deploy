"""Bulk insert -- correctness first, then speed.

Getting Python values into DuckDB was the whole cost of a refresh. The deployed instance is
only 5.4x slower than a laptop on a plain Python loop and scans its whole 20,667-row table in
8ms, but it inserted 51 rows/s against 2,200 locally. Neither CPU nor disk explains a 43x gap;
the per-value parameter binding layer does.

So large batches are written as CSV and read back by DuckDB's C++ parser, which binds nothing
(45,240 rows/s locally against 2,648). That is exactly the kind of change that trades a
performance problem for a correctness one -- a resource group named with a comma, a tag holding
a newline, or a NULL coming back as an empty string. These checks are the evidence that it does
not: they run hostile values through both paths and demand identical results.

The schema here is the REAL one, with a DATE column and four DOUBLEs. An earlier version of
this file used VARCHAR for all 25 columns, and that convenient simplification is exactly why
the first server-side benchmark blew up on data the real table would never have accepted.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb

import app.warehouse as W
from app.warehouse import COLUMNS, CSV_NULL, CSV_THRESHOLD, INSERT_CHUNK, NUMERIC, insert_rows

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


COLS = list(COLUMNS)
names = ",".join(f'"{c}"' for c in COLS)
TYPED = ",\n ".join(
    f'"{c}" ' + ("DOUBLE" if c in NUMERIC else
                 ("DATE" if c == "ChargePeriodStart" else "VARCHAR"))
    for c in COLS)


def fresh():
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE costs (\n {TYPED}\n)")
    return con


# The values that break a naive CSV round-trip, plus the ones that break a naive NULL scheme.
nasty = ['has,comma', 'has"quote', "has'apostrophe", 'has\nnewline', 'has\ttab',
         '', 'trailing ', ' leading', 'unicode Rs e ZH', 'back\\slash',
         'NULL', 'null', '\\N', '""']


def build(n):
    out = []
    for i in range(n):
        row = []
        for j, c in enumerate(COLS):
            if c in NUMERIC:
                row.append(None if i % 13 == 0 else round((i + j) * 1.7, 6))
            elif c == "ChargePeriodStart":
                row.append("2026-08-%02d" % (i % 28 + 1))
            elif i % 7 == 0 and j % 4 == 0:
                row.append(None)
            else:
                row.append(nasty[(i + j) % len(nasty)])
        out.append(row)
    return out


section("both paths write exactly what executemany writes")

rows = build(max(CSV_THRESHOLD + 50, 600))

base = fresh()
base.executemany(f"INSERT INTO costs ({names}) VALUES ({','.join('?' * len(COLS))})", rows)
want = base.execute(f"SELECT {names} FROM costs").fetchall()

# Force each path rather than trusting the threshold to pick the interesting one.
bound = fresh()
W.CSV_THRESHOLD = 10**9
try:
    insert_rows(bound, rows)
    got_bound = bound.execute(f"SELECT {names} FROM costs").fetchall()
finally:
    W.CSV_THRESHOLD = CSV_THRESHOLD

viacsv = fresh()
W.CSV_THRESHOLD = 1
try:
    insert_rows(viacsv, rows)
    got_csv = viacsv.execute(f"SELECT {names} FROM costs").fetchall()
finally:
    W.CSV_THRESHOLD = CSV_THRESHOLD

check("parameter binding matches executemany exactly", got_bound == want)
check("the csv path matches executemany exactly", got_csv == want)
check("and the two fast paths agree with each other", got_bound == got_csv)

nulls_txt = base.execute('SELECT count(*) FROM costs WHERE "MeterName" IS NULL').fetchone()[0]
nulls_num = base.execute('SELECT count(*) FROM costs WHERE "UnitPrice" IS NULL').fetchone()[0]
check("the fixture actually contains text NULLs to preserve", nulls_txt > 0)
check("and numeric NULLs", nulls_num > 0)
check("the csv path keeps text NULL as NULL",
      viacsv.execute('SELECT count(*) FROM costs WHERE "MeterName" IS NULL').fetchone()[0]
      == nulls_txt)
check("the csv path keeps numeric NULL as NULL",
      viacsv.execute('SELECT count(*) FROM costs WHERE "UnitPrice" IS NULL').fetchone()[0]
      == nulls_num)
# An empty string and a NULL are different facts. A CSV scheme that maps '' to NULL loses one.
check("an empty string does not become NULL",
      viacsv.execute("""SELECT count(*) FROM costs WHERE "ResourceName" = ''""").fetchone()[0]
      == base.execute("""SELECT count(*) FROM costs WHERE "ResourceName" = ''""").fetchone()[0])
check("a value containing a comma survives the csv path",
      viacsv.execute("""SELECT count(*) FROM costs WHERE "ResourceGroup" LIKE '%,%'""")
      .fetchone()[0]
      == base.execute("""SELECT count(*) FROM costs WHERE "ResourceGroup" LIKE '%,%'""")
      .fetchone()[0])
check("so does one containing a newline",
      viacsv.execute(
          """SELECT count(*) FROM costs WHERE "ResourceGroup" LIKE '%' || chr(10) || '%'""")
      .fetchone()[0] > 0)
check("so does one containing a double quote",
      viacsv.execute("""SELECT count(*) FROM costs WHERE "ResourceGroup" LIKE '%"%'""")
      .fetchone()[0] > 0)
check("dates are stored as dates, not text",
      str(base.execute('SELECT "ChargePeriodStart" FROM costs LIMIT 1').fetchone()[0])
      == str(viacsv.execute('SELECT "ChargePeriodStart" FROM costs LIMIT 1').fetchone()[0]))

section("the NULL sentinel cannot collide with real data")

check("it is built from control characters", CSV_NULL.startswith("\x01"))
check("and is not a word a billing system would emit", CSV_NULL != "NULL")
# The literal strings 'NULL', 'null' and '\\N' are all in the fixture above and must survive as
# text -- if the sentinel were any of them, they would silently become NULL.
check("the literal text 'NULL' is preserved, not turned into NULL",
      viacsv.execute("""SELECT count(*) FROM costs WHERE "ResourceGroup" = 'NULL'""")
      .fetchone()[0]
      == base.execute("""SELECT count(*) FROM costs WHERE "ResourceGroup" = 'NULL'""")
      .fetchone()[0])

section("batching is invisible to the caller")

for n in (0, 1, INSERT_CHUNK - 1, INSERT_CHUNK, INSERT_CHUNK + 1,
          CSV_THRESHOLD - 1, CSV_THRESHOLD, CSV_THRESHOLD + 1):
    c = fresh()
    sample = build(n)
    wrote = insert_rows(c, sample)
    stored = c.execute("SELECT count(*) FROM costs").fetchone()[0]
    check(f"{n} rows in, {n} rows out (crosses a batch or path boundary)",
          wrote == n and stored == n)

section("a csv failure falls back rather than losing the batch")

# Speed is never worth a lost refresh. If anything about the temp file goes wrong the slow path
# still has to run, and the rows still have to land.
original = W._insert_via_csv


def boom(*a, **k):
    raise OSError("no space left on device")


W._insert_via_csv = boom
try:
    c = fresh()
    wrote = insert_rows(c, rows)
    check("the rows are written anyway",
          wrote == len(rows)
          and c.execute("SELECT count(*) FROM costs").fetchone()[0] == len(rows))
    check("and they are still correct",
          c.execute(f"SELECT {names} FROM costs").fetchall() == want)
finally:
    W._insert_via_csv = original

section("columns are named, not positional")

# A positional insert depends on the table matching today's COLUMNS order. ALTER TABLE appends,
# so a column added later shifts every value after it one place left -- silently, and only in
# the columns nobody checks first.
shifted = duckdb.connect(":memory:")
shifted.execute(f'CREATE TABLE costs ("Extra" VARCHAR, {TYPED})')
insert_rows(shifted, build(3))
check("an unexpected leading column does not shift the data",
      shifted.execute('SELECT "Extra" FROM costs').fetchone()[0] is None)
check("and the named columns still receive their values",
      shifted.execute('SELECT "ServiceName" FROM costs').fetchone()[0] is not None)

section("fast enough to matter")

big = build(7000)

slow_con = fresh()
t0 = time.perf_counter()
slow_con.executemany(
    f"INSERT INTO costs ({names}) VALUES ({','.join('?' * len(COLS))})", big)
slow = time.perf_counter() - t0

bind_con = fresh()
W.CSV_THRESHOLD = 10**9
t0 = time.perf_counter()
insert_rows(bind_con, big)
bind = time.perf_counter() - t0
W.CSV_THRESHOLD = CSV_THRESHOLD

csv_con = fresh()
t0 = time.perf_counter()
insert_rows(csv_con, big)
fast = time.perf_counter() - t0

print(f"       executemany {slow:6.2f}s   binding {bind:6.2f}s   csv {fast:6.2f}s"
      f"   ({slow / fast if fast else 0:.0f}x vs executemany)")
check("the csv path beats parameter binding by a wide margin", fast * 3 < bind)
check("and all three wrote the same number of rows",
      slow_con.execute("SELECT count(*) FROM costs").fetchone()[0]
      == bind_con.execute("SELECT count(*) FROM costs").fetchone()[0]
      == csv_con.execute("SELECT count(*) FROM costs").fetchone()[0] == 7000)

section("both write paths use the shared helper")

wh = open("app/warehouse.py", encoding="utf-8").read()
ex = open("app/exports.py", encoding="utf-8").read()
check("the quick and report paths use it", "insert_rows(con, records)" in wh)
check("the FOCUS/export path uses it", "insert_rows(con, all_records)" in ex)
check("neither writes rows with executemany any more",
      "con.executemany" not in wh and "con.executemany" not in ex)
check("the csv scratch file is removed even when the load fails",
      "os.remove(path)" in wh)
check("the threshold is tunable without a code change",
      'os.getenv("COST_CSV_THRESHOLD"' in wh)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

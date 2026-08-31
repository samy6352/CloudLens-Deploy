"""Export selection: cover the month people are looking at.

Two bugs, one root cause -- treating Cost Management exports as interchangeable.

A MonthToDate export is rewritten daily and covers the month in progress. A TheLastMonth
export is written once a month and covers only closed months. They are complementary, not
alternatives, and this estate has both.

The refresh loaded the first export that worked and stopped. That happened to be the
closed-month one, so pressing "FOCUS report" rewrote June and July and left August exactly as
it was. Nothing failed and nothing said so. After a quick refresh -- which cannot fetch tags --
the current month kept its tagless rows, so Cost by Tag stayed empty and looked like a broken
export. The export was fine; the refresh simply never touched the month being displayed.

The nightly job had a plainer version of the same blindness: it called ingest_export() with no
account, container or SAS at all, which raises before reading anything. It had never fired --
the first run was the following morning -- so nobody had seen it fail yet.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.exports import covers_current_period

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


section("which timeframes include today")

for tf in ("MonthToDate", "BillingMonthToDate", "TheCurrentMonth", "WeekToDate"):
    check(f"{tf} covers the current period", covers_current_period({"timeframe": tf}))
# These are the ones that silently leave the current month alone.
for tf in ("TheLastMonth", "TheLastBillingMonth", "TheLastWeek"):
    check(f"{tf} does not", not covers_current_period({"timeframe": tf}))
check("an unknown timeframe is treated as not covering today",
      not covers_current_period({"timeframe": "Custom"}))
check("a missing timeframe is treated the same way",
      not covers_current_period({}))

section("the refresh loads one from each group, not just the first that works")

main_py = open("app/main.py", encoding="utf-8").read()

check("selection is factored into one place both callers use",
      "async def _load_export_pair" in main_py)
check("exports are split by whether they cover the current period",
      "if covers_current_period(c)" in main_py
      and "if not covers_current_period(c)" in main_py)
check("the current period is attempted first",
      main_py.index("got_current = await first_that_works(current)")
      < main_py.index("got_closed = await first_that_works(closed)"))
# Both groups must be attempted. A `return` after the first success is exactly the bug.
check("a successful current-period load does not skip the closed months",
      "got_closed = await first_that_works(closed)" in main_py)
check("which exports were loaded is reported",
      '"exports_loaded"' in main_py)
# ingest_export marks the run ready as its last act and the archive step after it takes long
# enough that a poll can land in between, seeing a finished refresh with no record of what it
# loaded. Recorded per success, not only in a summary at the end.
check("and recorded as each one succeeds, not only at the end",
      main_py.count('"exports_loaded"') >= 2)
check("recorded before the archive step, not after it",
      main_py.index('"exports_loaded": list(loaded)')
      < main_py.index("await _archive_after_ingest(\n                    candidate"))
# Archiving is a follow-up. It was inside the same try as the ingest, so a failure writing the
# parquet snapshot would have discarded a perfectly good refresh and moved on to the next
# export to do the whole thing again.
check("an archive failure does not discard a successful load",
      "the data is already loaded" in main_py)
# Loading only closed months is a legitimate outcome -- but it means today did not move, and
# saying nothing makes a stale figure look like a fresh one.
check("loading only closed months says today's figures are unchanged",
      "today's figures are unchanged" in main_py)

section("the nightly refresh can actually read something")

# ingest_export() with no account, container or SAS raises before reading anything, so every
# night would have logged "Provide either sas_url, or both account and container" and stopped.
check("it no longer calls ingest_export with no source at all",
      "ingest_export(max_files=60)" not in main_py)
check("it discovers exports the way the button does",
      "found = await discover_exports()" in main_py)
check("it checks reachability before starting",
      "ok, _bad = await reachable(usable)" in main_py)
check("it loads the same complementary pair",
      "_load_export_pair(ok, max_files=60)" in main_py)
check("a configured SAS is still honoured directly",
      'os.getenv("COST_EXPORT_SAS_URL"' in main_py)
check("no readable export skips the night rather than failing loudly",
      "daily refresh skipped: no readable export" in main_py)

section("the functions the routes call actually exist")

# Twice now an edit that inserted a new function above an existing one swallowed the existing
# one's `def` line, leaving its body attached to the new function's. Python imports that
# happily -- the orphaned docstring and code just become dead statements -- so nothing failed
# until a request arrived and raised NameError as a bare 500. A cheap existence check catches
# it at test time instead.
import asyncio  # noqa: E402
import inspect  # noqa: E402

import app.main as m  # noqa: E402

for name in ("_run_ingest", "_load_export_pair", "_daily_refresh", "_archive_after_ingest",
             "start_ingest", "ingest_from_export", "warehouse_status", "diagnostics",
             "diagnostics_export", "dashboard_tags", "_tags_missing_from_load"):
    fn = getattr(m, name, None)
    check(f"{name} is defined", callable(fn))

check("_run_ingest still takes the arguments the route passes",
      list(inspect.signature(m._run_ingest).parameters) == ["months", "metric", "quick"])
check("_load_export_pair is a coroutine the callers can await",
      asyncio.iscoroutinefunction(m._load_export_pair))

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

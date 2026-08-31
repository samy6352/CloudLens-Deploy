"""
Checks on the Refresh control.

Every bug this covers presented the same way: you click Refresh, the spinner runs, it stops, and
the numbers are unchanged with no error anywhere. That is the worst failure mode this app has,
because it is indistinguishable from the button not being wired up at all.

  * ingest() ended with `{**state, "status": "ready", **summary()}`. summary() carries no status
    of its own, so "ready" was hardcoded: an ingest where every subscription failed still
    reported success, and the UI -- which only ever tested status == "failed" -- silently redrew
    the same data.

  * The finished-state message was written to `error` by the export path and `detail` by the API
    path, and the export handler merged into the previous state. A stale `detail` from an earlier
    run could therefore outrank the current `error` and describe the wrong failure.

  * Outcomes were reported with notify(), which prepends a banner into #tabBody. On a partial
    refresh the loadTabs() immediately after wiped it before it could be read.

Run: python test_refresh.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/project")

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "web" / "assets"

checks = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


# ------------------------------------------------------- ingest outcome status
print("\nwarehouse.ingest — reports what actually happened")

from app.warehouse import Warehouse  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "t.duckdb"
wh = Warehouse(db_path=tmp)

SUBS = [{"id": "s1", "name": "Sub One"}, {"id": "s2", "name": "Sub Two"}]


def run_ingest(behaviour) -> dict:
    """Drive ingest with _report replaced, so nothing touches the network."""
    async def fake_report(client, sub, start, end, headers, metric="AmortizedCost"):
        return behaviour(sub, start)

    original = wh._report
    wh._report = fake_report  # type: ignore[assignment]
    try:
        return asyncio.run(wh.ingest(SUBS, months=1))
    finally:
        wh._report = original  # type: ignore[assignment]


def boom(sub, start):
    raise RuntimeError(f"no route to host for {sub}")


state = run_ingest(boom)
check("all jobs failing reports 'failed', not 'ready'", state["status"] == "failed",
      f"got {state['status']!r}")
check("failed state says why", "no route to host" in (state.get("detail") or ""),
      repr(state.get("detail"))[:120])
check("failed count is preserved", state.get("failed") == state.get("total"),
      f"failed={state.get('failed')} total={state.get('total')}")


def half(sub, start):
    if sub == "s1":
        raise RuntimeError("throttled after several retries")
    return []  # no links: a clean run that simply had no data


state = run_ingest(half)
check("some jobs failing reports 'partial'", state["status"] == "partial",
      f"got {state['status']!r}")
check("partial state names the failure", "throttled" in (state.get("detail") or ""),
      repr(state.get("detail"))[:120])
check("partial state counts the failures", 0 < state.get("failed", 0) < state.get("total", 0))

state = run_ingest(lambda sub, start: [])
# A report that returns nothing is no longer "ready". It used to be, and that was the bug: the
# period had already been deleted by then, so an empty report destroyed the data it was meant to
# replace and then reported success. Nothing is deleted now, so the honest status is "empty" —
# the refresh ran, nothing came back, and what was there is still there.
check("a run that loaded nothing reports 'empty', not 'ready'",
      state["status"] == "empty", f"got {state['status']!r}")
check("it explains that the existing data was left alone",
      "unchanged" in (state.get("detail") or ""), repr(state.get("detail"))[:120])
check("a clean run reports zero failures", state.get("failed") == 0)


# ------------------------------------------------- a failed refresh must not destroy data
print("\nwarehouse.ingest — a failed refresh leaves the data it could not replace")

ROW = {
    "ChargePeriodStart": "2026-08-01", "BilledCost": 12.5, "BillingCurrency": "USD",
    "SubAccountId": "s1", "SubAccountName": "Sub One",
}


def seed(wh_obj, sub_id: str, day: str, n: int) -> None:
    """Put rows in the warehouse the way a previous successful refresh would have."""
    cols = list(__import__("app.warehouse", fromlist=["COLUMNS"]).COLUMNS)
    names = ",".join(f'"{c}"' for c in cols)
    holes = ",".join("?" * len(cols))
    rec = []
    for c in cols:
        if c == "ChargePeriodStart":
            rec.append(day)
        elif c == "BilledCost":
            rec.append(1.0)
        elif c == "SubAccountId":
            rec.append(sub_id)
        elif c == "SubAccountName":
            rec.append("Sub One")
        elif c == "BillingCurrency":
            rec.append("USD")
        else:
            rec.append(None)
    with wh_obj.connect() as con:
        con.executemany(f"INSERT INTO costs ({names}) VALUES ({holes})", [rec] * n)


tmp2 = Path(tempfile.mkdtemp()) / "keep.duckdb"
wh2 = Warehouse(db_path=tmp2)
seed(wh2, "s1", "2026-08-05", 40)
before = wh2.summary()["rows"]


def run_on(wh_obj, behaviour) -> dict:
    async def fake_report(client, sub, start, end, headers, metric="AmortizedCost"):
        return behaviour(sub, start)

    original = wh_obj._report
    wh_obj._report = fake_report  # type: ignore[assignment]
    try:
        return asyncio.run(wh_obj.ingest([{"id": "s1", "name": "Sub One"}], months=1))
    finally:
        wh_obj._report = original  # type: ignore[assignment]


state = run_on(wh2, boom)
after = wh2.summary()["rows"]
check("a refresh whose report fails keeps every row it already had",
      after == before == 40, f"{before} -> {after}")
check("and reports the failure rather than success", state["status"] == "failed")

state = run_on(wh2, lambda sub, start: [])
after_empty = wh2.summary()["rows"]
check("a report that returns nothing also keeps the rows",
      after_empty == 40, f"40 -> {after_empty}")

# The delete must not run before the data is in hand -- that is what emptied the warehouse for
# the whole of a multi-minute refresh and lost it outright when the report failed.
import inspect as _inspect  # noqa: E402

src_one = _inspect.getsource(Warehouse.ingest)
check("the period is not cleared before the report is fetched",
      "DELETE FROM costs" not in src_one, "ingest() still deletes inline")
check("replacement is a single transaction",
      "BEGIN TRANSACTION" in _inspect.getsource(Warehouse._replace))
check("the swap rolls back if the insert fails",
      "ROLLBACK" in _inspect.getsource(Warehouse._replace))
check("fetching no longer writes to the database",
      "INSERT INTO costs" not in _inspect.getsource(Warehouse._fetch))


# --------------------------------------------- Azure's own failures are explained, not dumped
print("\nwarehouse — an Azure-side failure reads as one")

from app.warehouse import SUBMIT_BUDGET, _report_failure  # noqa: E402

five_hundred = {"status": "Failed", "error": {
    "code": 500,
    "message": "InternalServerError occurred during processing the operation Id:cc829636."}}
msg = _report_failure(five_hundred, "ActualCost")
check("a 500 is attributed to Azure, not to this app",
      "fault on Azure's side" in msg, msg[:80])
check("it names the metric that failed", "ActualCost" in msg)
check("it suggests the metric that is generated separately", "Amortized" in msg)
check("the raw correlation id is not the whole message", "cc829636" not in msg)

named = {"status": "Failed", "error": {"code": "BadRequest", "message": "Invalid time period."}}
check("a specific error is passed through rather than guessed at",
      "Invalid time period." in _report_failure(named, "ActualCost"))
check("an error with no message still says something",
      _report_failure({"status": "Failed"}, "ActualCost") != "")

# Throttling used to sleep 60+120+180+180+180s before the first poll even started.
check("submission throttling is bounded in total, not by attempt count",
      SUBMIT_BUDGET <= 300, f"budget={SUBMIT_BUDGET}s")
src_report = _inspect.getsource(Warehouse._report)
check("the submission budget is actually enforced", "SUBMIT_BUDGET" in src_report)
check("exhausted throttling explains it is a per-subscription limit",
      "per-" in src_report and "subscription limit" in src_report)

# A period is minutes of waiting; without a phase the UI has nothing to show but 0/N.
check("the wait is reported as a phase", "_phase" in src_report)
check("phases cover both waits",
      '"submitting"' in src_report and '"generating"' in src_report)
check("the dashboard shows the phase",
      "generating" in (HERE / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8"))


# ------------------------------------------------- submissions are queued, not fired in a burst
print("\nwarehouse — submissions are spaced so they are not refused")

from app.warehouse import SUBMIT_GAP  # noqa: E402

check("submissions are serialised per subscription", "_submit_slot" in src_report)
check("the slot is per subscription, not global",
      "sub" in _inspect.signature(Warehouse._submit_slot).parameters)
slot_src = _inspect.getsource(Warehouse._submit_slot)
check("a minimum gap is enforced between submissions", "SUBMIT_GAP" in slot_src)
check("the gap is short enough to stay out of the way", 0 < SUBMIT_GAP <= 15, f"{SUBMIT_GAP}s")
# Only the POST is queued. Serialising the polling too would make a refresh take the *sum* of
# its reports rather than roughly the longest one.
check("only submission is serialised, not generation",
      "client.post" in src_report.split("_submit_slot")[1][:200], "slot wraps the POST")
check("polling happens outside the slot",
      src_report.index("_phase(1, \"generating\")") > src_report.rindex("_submit_slot"))


async def _timed_slot() -> float:
    """Two submissions for one subscription must be spaced; two subscriptions must not be."""
    wh_t = Warehouse(db_path=Path(tempfile.mkdtemp()) / "slot.duckdb")
    import time as _t

    t0 = _t.monotonic()
    async with wh_t._submit_slot("subA"):
        pass
    async with wh_t._submit_slot("subA"):
        pass
    same = _t.monotonic() - t0

    t1 = _t.monotonic()
    await asyncio.gather(*(_hold(wh_t, f"sub{i}") for i in range(3)))
    across = _t.monotonic() - t1
    return same, across


async def _hold(wh_obj, sub):
    async with wh_obj._submit_slot(sub):
        return None


same, across = asyncio.run(_timed_slot())
check("a second submission for the same subscription waits its turn",
      same >= SUBMIT_GAP * 0.9, f"{same:.1f}s for two")
check("different subscriptions are not made to queue behind each other",
      across < SUBMIT_GAP * 0.5, f"{across:.1f}s for three subs")


# ----------------------------------------------- FOCUS answers immediately when it cannot work
print("\nFOCUS — an unreadable export is refused up front, not after a spinner")

from app.exports import reachable  # noqa: E402

check("there is a reachability pre-flight", callable(reachable))
ingest_src = _inspect.getsource(main.ingest_from_export) if "main" in dir() else ""


# The metric has to survive into the state, or there is no way to tell which view was loaded.
check("state records the metric used", state.get("metric") == "AmortizedCost",
      repr(state.get("metric")))


# ------------------------------------------------------------------ endpoints
print("\n/api/ingest — argument validation")

from fastapi.testclient import TestClient  # noqa: E402
import app.main as main  # noqa: E402

# Never let the test start a real background job.
started: list[tuple] = []


async def _noop(months: int, metric: str = "AmortizedCost", quick: bool = False) -> None:
    started.append((months, metric, quick))


main._run_ingest = _noop  # type: ignore[assignment]

with TestClient(main.app) as client:
    r = client.post("/api/ingest?months=1&metric=Nonsense")
    check("an unknown metric is refused up front", r.status_code == 400, f"got {r.status_code}")
    check("the refusal explains the allowed values",
          "AmortizedCost" in r.text and "ActualCost" in r.text)

    for metric in ("AmortizedCost", "ActualCost"):
        r = client.post(f"/api/ingest?months=3&metric={metric}")
        check(f"{metric} is accepted", r.status_code == 200, f"got {r.status_code}")
        check(f"{metric} is echoed back", r.json().get("metric") == metric)

    # The quick flag has to survive the query string, the handler and the task arguments --
    # a default that silently stayed False would make the fast option quietly slow.
    r = client.post("/api/ingest?months=3&metric=ActualCost&quick=true")
    check("a quick refresh is accepted", r.status_code == 200, f"got {r.status_code}")
    check("and is echoed back as quick", r.json().get("quick") is True)

    r = client.get("/api/warehouse")
    check("/api/warehouse serves ingest state", r.status_code == 200 and "ingest" in r.json())

check("the metric reaches the background job",
      started and started[-1][1] == "ActualCost", repr(started[-1:]))
check("and so does the quick flag",
      started and started[-1][2] is True, repr(started[-1:]))
check("a normal refresh is not quietly turned into a quick one",
      any(s[2] is False for s in started), repr(started))


# ------------------------------------------------------------ the menu itself
print("\ndashboard.js — the Refresh menu")

dash = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

menu = re.search(r"function buildRefreshMenu\(\)\s*\{(.*?)\n\}", dash, re.S)
check("buildRefreshMenu exists", menu is not None)
body = menu.group(1) if menu else ""

buttons = re.findall(r"<button data-act=", body)
check("the menu offers exactly four choices", len(buttons) == 4, f"found {len(buttons)}")
for label in ("Quick refresh", "FOCUS report", "Amortized", "Actual"):
    check(f"offers '{label}'", label in body)
# The two slow ones say why they are slow, and the quick one says what it costs. A menu where
# the difference is only in the verb picked leaves someone choosing at random.
check("the slow options are marked as full detail", body.count("full detail") == 2)
check("and the quick one is honest about the trade",
      "capped at about a minute" in body and "no quantities," in body)

# The old menu listed every discovered export and fired a live Azure call on every open, which
# took ~13s on this tenant before anything was clickable.
check("opening the menu makes no Azure call", "/api/exports" not in body or
      "/api/exports/ingest" in body and "get(\"/api/exports\"" not in dash)
check("no per-export picker remains", "export_id: e.id" not in dash)
check("the menu is built once", "dataset.built" in dash)
check("FOCUS lets the server choose the export", 'startIngest("/api/exports/ingest"' in dash)
check("the period is fixed rather than asked", "months=3" in dash and "data-months" not in dash)

watch = re.search(r"async function watchIngest\(\)\s*\{(.*?)\n\}", dash, re.S)
wbody = watch.group(1) if watch else ""
# Comments are stripped first: an earlier version of this check matched the word "notify"
# inside the comment explaining why notify is not used here.
wbare = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", wbody, flags=re.S))
check("watchIngest handles 'partial'", '"partial"' in wbare)
check("watchIngest handles 'failed'", '"failed"' in wbare)
# A banner lives in #tabBody, which loadTabs() replaces — outcomes must float instead.
check("outcomes use toast, not a tab banner", "toast(" in wbare and "notify(" not in wbare)
check("a timeout is reported rather than silent",
      "stopped watching" in wbare or "still running on the server" in wbare)
check("failure prefers the current detail", "state.detail" in wbare)
# notify() was left behind with no callers once outcomes moved to toasts.
check("the unused banner helper is gone", "function notify(" not in dash)

# ---------------------------------------------------------------- export reads
# A FOCUS export is a couple of very large blobs, and essentially all of the wall time is one
# download. Reporting only on completion left the bar at 0% for over five minutes and then
# jumped it to 90% — from the outside, indistinguishable from a refresh that has hung. This is
# the whole of "the refresh button doesn't work": it was working, silently.
exp = (HERE / "app" / "exports.py").read_text(encoding="utf-8")
check("a file is claimed before it is downloaded, not after",
      exp.index('warehouse.state["fetching"] = n') < exp.index("await reader.get(b[\"name\"])"),
      "progress must move when work starts")
check("the bar advances to the start of that file's share",
      '((n - 1) / len(latest)) * 0.9' in exp)
check("and the marker is cleared once the loop ends",
      'warehouse.state.pop("fetching", None)' in exp)
check("the file being fetched is named", 'fetching_name' in exp and 'fetching_name' in dash)
check("with its size, so a long wait is explained",
      'fetching_bytes' in exp and 'fetching_bytes' in dash)
check("the UI says Downloading while a fetch is in flight",
      "Downloading file ${state.fetching}" in dash)

# The actual cause of "the refresh button doesn't work". Both DuckDB writes and the CSV parse
# ran on the event loop, so a refresh froze the entire app: the log shows the last request
# served at 11:15:38 and the next at 11:20:51, five minutes with nothing in between. The bar
# could not move because the poll that moves it could not be answered.
check("the export write is off the event loop", "await asyncio.to_thread(_write)" in exp)
check("and so is parsing every row of the CSV",
      "await asyncio.to_thread(_load_rows" in exp)
wh = (HERE / "app" / "warehouse.py").read_text(encoding="utf-8")
check("the API path's write is off the event loop too",
      "await asyncio.to_thread(_swap)" in wh)
check("the writer lock is still held around it",
      wh.index("async with self._lock:") < wh.index("await asyncio.to_thread(_swap)"))
check("the write phase is named in the state", '"writing"' in exp)
check("and the UI says what it is doing", "state.writing" in dash
      and "Writing ${state.writing" in dash)

# Concurrency the freeze had been hiding. DuckDB shares one database instance per process and
# refuses a second connection whose configuration differs, so the moment reads and writes could
# actually overlap, every read during an ingest failed with "different configuration than
# existing connections" — a 500 on /api/warehouse, which is the poll that draws the bar.
check("every connection opens with the same configuration",
      "duckdb.connect(str(self.db_path))" in wh)
check("read_only is not passed through to DuckDB",
      "read_only=read_only" not in wh)
check("and the reason is written down where the next person will look",
      "different configuration" in wh)

# Opening that connection per operation was the next failure: constructing and destroying the
# database instance around every read and write meant an open could race a close, and DuckDB
# refused it with "Unique file handle conflict". On /home — an Azure Files share, where the
# handle outlives close() — that cost two of nine periods on a real amortized refresh.
check("the file is opened once, behind a guard",
      "def _database(self)" in wh and "self._db_lock" in wh)
check("and every caller gets a cursor on it rather than a new connection",
      "self._database().cursor()" in wh)
check("the double-check is inside the lock, not just outside it",
      wh.index("with self._db_lock:") < wh.index('self._db = duckdb.connect(str(self.db_path))'))
check("the guard is a threading lock, because ingest opens from worker threads",
      "import threading" in wh)
check("and the reason that costs real rows is written down",
      "Unique file handle conflict" in wh)

# The two refresh routes count different things, and calling both "periods" put
# "0/2 periods loaded" over a FOCUS run that has no periods in it.
check("an export read counts files", 'warehouse.state["unit"] = "file"' in exp)
check("and the UI uses whichever unit the server reported",
      'state.unit || "period"' in dash)
check("the count is hidden while a single file is in flight",
      "total && !state.fetching" in dash)

# --------------------------------------------------------------- schedule fixes
# Creating an export with a managed identity makes Azure grant that identity a role on the
# destination on the caller's behalf, so the caller needs roleAssignments/write there. Azure
# reports the refusal as HTTP 401 RBACAccessDenied — which reads exactly like an expired token,
# and was being reported as a missing Cost Management *Reader* role on a *write*.
sched = (HERE / "app" / "schedules.py").read_text(encoding="utf-8")
check("the roleAssignments/write refusal is explained in its own words",
      "roleassignments/write" in sched.lower().replace(" ", ""))
check("and names the role that actually fixes it",
      "User Access Administrator" in sched)
check("and warns that the grant takes a few minutes",
      "few minutes" in sched)
check("the raw Azure refusal is logged before it is turned into a sentence",
      "export PUT %s failed" in sched)

cost_py = (HERE / "app" / "cost.py").read_text(encoding="utf-8")
check("401 and 403 are no longer reported as the same problem",
      'if r.status_code == 403:' in cost_py)
check("a 401 says the credential was refused, not that a role is missing",
      "rejected the credential" in cost_py)
check("and Azure's own explanation is kept rather than discarded",
      "r.text" in cost_py and "WWW-Authenticate" in cost_py)

# FOCUS was missing from the schedule options because the API version was too old, not because
# Azure disallows it at subscription scope. 2023-08-01 answers a FocusCost body with
# "Invalid definition type 'FocusCost'" -- a 400 naming the type rather than the version -- and
# it also omits existing FOCUS exports from its list. 2025-03-01 creates one and lists them.
# Both verified against the live tenant on 2026-08-30.
check("the export API is new enough to know FocusCost",
      'EXPORT_API = "2025-03-01"' in sched)
check("so FOCUS is offered as a schedulable report",
      '"FocusCost"' in sched and "FocusCost" in sched.split("METRICS = ")[1].split(")")[0])
check("and it leads, being the schema that carries both costs",
      sched.split("METRICS = ")[1].lstrip().startswith('("FocusCost"'))
check("the tab offers it too",
      '<option value="FocusCost">' in dash)
check("and no longer claims Azure refuses it",
      "FOCUS cannot be scheduled" not in dash
      and "Only ActualCost and" not in sched)
check("an unknown type now blames the version, which is what it is",
      "API version this request" in sched)
# Discovery had the same blind spot: the GA versions it asked omit FocusCost entirely, so an
# export created in the portal was invisible and the refresh reported no FOCUS available.
exp_py = (HERE / "app" / "exports.py").read_text(encoding="utf-8")
check("discovery asks the version that can see FOCUS exports",
      '"2025-03-01"' in exp_py.split("EXPORT_API_VERSIONS = ")[1].split(")")[0])
check("and asks it first",
      exp_py.split("EXPORT_API_VERSIONS = ")[1].lstrip().startswith('("2025-03-01"'))

# Fourteen schedules in alphabetical order buried the one just created. Newest first, so the
# answer to "did that work?" is at the top where the eye already is.
check("schedules come back newest first",
      bool(re.search(r'found\.sort\(key=lambda e: \(e\["starts"\][^)]*\), reverse=True\)', sched)))
# The two timestamps were both mislabelled: `recurrencePeriod.from` (the start) was called
# next_run, and nextRunTimeEstimate (the real next run) was called last_run and never read.
check("the schedule start is named for what it is",
      '"starts": (sched.get("recurrencePeriod") or {}).get("from")' in sched)
check("and the next run is no longer called the last one",
      '"next_run": p.get("nextRunTimeEstimate")' in sched
      and '"last_run"' not in sched)
check("the next run is actually shown, having been fetched all along",
      "s.next_run ? `next ${String(s.next_run)" in dash)

# ------------------------------------------------------- the quick refresh path
main_py = (HERE / "app" / "main.py").read_text(encoding="utf-8")
# The detail report is asynchronous: Azure builds it, which is 40s+ per period behind a
# per-subscription throttle, so nine periods ran ten to twenty minutes and once hit the
# twenty-minute ceiling. The Query API answers the same question synchronously -- measured at
# 7.8s for a month of a subscription, 1,622 rows, no continuation.
check("there is a synchronous path that does not wait for a report",
      "async def quick_ingest" in wh and "def _query_period" in wh)
check("it calls the query endpoint, not generateCostDetailsReport",
      "CostManagement/" in wh and 'f"query?api-version={QUERY_API}"' in wh)
check("it asks for daily granularity, or the rows are a single lump per month",
      '"granularity": "Daily"' in wh)
check("it retries the throttle rather than failing the period",
      "if r.status_code != 429" in wh.split("async def _query_period")[1].split("async def")[0])
check("and follows the continuation, so a big month is not silently truncated",
      'props.get("nextLink")' in wh)
# Replacing detailed rows with summary ones is a real trade, so it is named and reported
# rather than left for someone to discover via an empty Tags tab.
check("the columns a query cannot fill are listed in one place",
      "QUICK_OMITS = (" in wh)
check("the finished state says it was a quick load",
      '"mode": "quick"' in wh)
check("and the UI says what that cost", 'state.mode === "quick"' in dash)
check("the quick option is offered", 'data-act="quick"' in dash)
# It used to lead, on the claim that it took seconds. It does not: the Query API is
# rate-limited hard enough that a busy day still takes minutes, so the export path — a blob
# read, with no report to build and no quota to spend — is the one worth reaching for first.
check("but the export path leads, being genuinely the fastest",
      dash.index('data-act="focus"') < dash.index('data-act="quick"'))
check("the endpoint takes the flag", "quick: bool = False" in main_py)
check("and routes it to the quick path",
      "await warehouse.quick_ingest(" in main_py)
# Every period is still swapped in one transaction, so a quick load that fails part-way leaves
# what was already held rather than half-replacing it.
check("a quick load replaces periods the same transactional way",
      "await self._replace(" in wh.split("async def quick_ingest")[1])
check("and an empty answer keeps the rows already held",
      "kept the " in wh.split("async def quick_ingest")[1])
# Measured live before this was fixed: 0 of 9 periods done after 198 seconds. Two causes, both
# already solved once for the report path and not carried across.
#
# The throttle is per subscription, so firing one estate's three months at once loses a race
# the caller started with itself -- the same reason report submissions queue per subscription.
check("queries queue per subscription rather than racing",
      "async def _query_slot" in wh and "self._query_locks" in wh)
check("and different subscriptions still run in parallel",
      "self._query_locks.setdefault(sub, asyncio.Lock())" in wh)
check("the quick path actually uses the slot",
      "async with self._query_slot(" in wh)
# And a 429 here costs seconds to clear, not the minute a report submission needs. Borrowing
# _retry_after's 60s opener made the fast path slower than the slow one.
check("the query backoff is its own, not the report's",
      "def _query_wait" in wh and "QUERY_BACKOFF = (" in wh)
check("it starts in seconds, not a minute",
      wh.split("QUERY_BACKOFF = (")[1].split(")")[0].strip().startswith("2.0"))
# The ladder is short on purpose. Waiting out a 429 is right for a background job and exactly
# wrong here: the old one could sleep 161s per subscription, which made the fast option the
# slowest in the menu. A quick refresh is a promise about time, so it has a budget.
check("and the whole ladder cannot outlast the budget",
      "QUICK_DEADLINE" in wh and "QUICK_REFRESH_SECONDS" in wh)
check("the deadline is enforced inside the query, not just around it",
      "deadline: float | None = None" in wh and "def out_of_time()" in wh)
check("running out of time is not reported as a failure",
      "except TimeoutError" in wh)
# "API data" was the old name for the full-detail Actual option. Left in the progress strip, a
# nine-period six-minute run was labelled as though it were the live query someone had meant to
# pick — which is exactly the confusion the rename was supposed to end.
check("the progress strip names the route, not the old option name",
      'ActualCost: "Actual" }' in dash and 'ActualCost: "API data"' not in dash)
check("and says which route is running",
      'state.mode === "quick"' in dash and '"full detail"' in dash)
check("the expected wait matches the route rather than being one sentence for all three",
      "ingestNote" in dash and "capped at about a minute" in dash
      and "ten minutes or more" in dash)
check("the quick path uses that backoff and not the report one",
      "_query_wait(r, attempt)" in wh.split("async def _query_period")[1].split("async def")[0])
check("a service-supplied Retry-After is still capped for the fast path",
      "60.0)" in wh.split("def _query_wait")[1].split("def ")[0])
# The real cost was call *count*, not wait length. A report must be generated per period, so
# `ingest` has no choice; a query does not -- one call returned all 91 days here. Nine racing
# requests against a per-subscription throttle produced eight failures.
check("the quick path asks once per subscription, not once per month",
      "spans = _periods(months)" in wh
      and "start, end = spans[0][0], spans[-1][1]" in wh)
check("so its progress is counted in subscriptions",
      '"unit": "subscription"' in wh)
check("and the UI reads whichever unit was reported",
      'state.unit || "period"' in dash)
# A raw 429 body invites the one response that makes it worse -- pressing Refresh again, which
# spends the quota that has just run out.
check("a throttled query is explained rather than dumped",
      "def _query_failure" in wh and "rate-limiting queries" in wh)
check("and points at the route that does not touch the limit",
      "FOCUS report reads blob storage" in wh)
check("the quick path uses that explanation",
      "_query_failure(r)" in wh)

# Cost Management throttles on several independent dimensions, and the one that was actually
# refusing us was *client type*. A caller that does not name itself shares a "DefaultQuota"
# bucket with every other anonymous client on the tenant. Measured live while all three
# subscriptions were failing: clienttype-requests DefaultQuota:0, while entity (3), tenant (19)
# and QPU (11 per 10s) all still had room. The identical request sent with a ClientType header
# was served immediately. Naming ourselves is the fix; without it we queue behind the portal.
check("queries identify this app so they get their own throttle bucket",
      "CLIENT_TYPE = os.getenv" in wh and '"ClientType": CLIENT_TYPE' in wh)
check("both the query and the report path send it",
      wh.count('"ClientType": CLIENT_TYPE') == 2)
check("the identity is overridable without a code change",
      'os.getenv("COST_CLIENT_TYPE"' in wh)
# The service told us exactly how long to wait -- in a header we were not reading. Only
# "Retry-After" and the *entity* variant were consulted, so a 429 carrying precise guidance
# ("wait 9 seconds") looked like it carried none and fell through to a ladder that guessed
# minutes. That guess is what made a throttled quick refresh feel like a dead end.
check("the client-type retry hint is read, not just the entity one",
      "clienttype-retry-after" in wh)
check("it is preferred over the less specific headers",
      wh.index("clienttype-retry-after") < wh.index("entity-retry-after"))
check("every waiter reads the same list rather than keeping its own",
      wh.count("for header in RETRY_HEADERS") == 3)
# The old wording said the limit "refills over several minutes". That was wrong in both
# directions: the limit that fires refills in seconds, and the advice talked people out of a
# retry that would have worked. Quote the service instead of guessing.
check("a throttle message quotes the service's own wait",
      "Azure asks for about" in wh)
check("and no longer invents a recovery time",
      "refills over several minutes" not in wh)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

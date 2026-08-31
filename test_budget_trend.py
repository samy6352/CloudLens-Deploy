"""The budget chart: its period, its filter, and the SQL that feeds it.

Azure reports a budget as one number with no history. These charts are built from the
warehouse instead, which means two things have to be right or the line contradicts the
headline it sits under: the *period* (Azure spans years but resets every grain) and the
*filter* (a tag-scoped budget must not be drawn over whole-subscription spend).
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.main as main
from app.warehouse import COLUMNS, NUMERIC, Warehouse

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


print("the current period, not the decade the budget was created for")

# The real shape from the tenant: created 2026-08-01, runs to 2036.
start, end = main._budget_period(
    {"time_grain": "Monthly", "start": "2026-08-01", "end": "2036-08-01"})
check("a monthly budget starts at the current month, not 2026-08 forever",
      start >= "2026-08-01", f"got {start}")
check("and ends today, not in 2036", end == date.today().isoformat(), f"got {end}")

# A budget that began years ago must still land on the month in progress.
start, _ = main._budget_period(
    {"time_grain": "Monthly", "start": "2024-05-01", "end": "2034-10-01"})
today = date.today()
check("an old monthly budget rolls forward to this month",
      start == today.replace(day=1).isoformat(), f"got {start}")

# Quarterly anchors on the budget's own start, not the calendar quarter.
qs, _ = main._budget_period(
    {"time_grain": "Quarterly", "start": "2026-02-01", "end": "2036-02-01"})
check("a quarterly budget anchors on its own start date",
      qs in ("2026-08-01", "2026-05-01"), f"got {qs}")

ys, _ = main._budget_period(
    {"time_grain": "Annually", "start": "2026-03-01", "end": "2036-03-01"})
check("an annual budget uses its own anniversary", ys == "2026-03-01", f"got {ys}")

# Bad input must not spin or explode.
bad, _ = main._budget_period({"time_grain": "Monthly", "start": None, "end": None})
check("a missing start date falls back to this month",
      bad == today.replace(day=1).isoformat(), f"got {bad}")
junk, _ = main._budget_period({"time_grain": "Weekly", "start": "not-a-date", "end": ""})
check("an unparseable start date does not raise", bool(junk))

print("\nthe filter, so a tag budget is not drawn over the whole subscription")

check("no filter means no tag clauses", main._budget_tags(None) == [])
one = main._budget_tags({"tags": {"name": "contact", "operator": "In", "values": ["x@y.z"]}})
check("a single tag clause is read", one == [("contact", ["x@y.z"])], f"got {one}")

both = main._budget_tags({"and": [
    {"tags": {"name": "env", "values": ["prod"]}},
    {"tags": {"name": "team", "values": ["core", "data"]}},
]})
check("an AND of tags keeps both", both == [("env", ["prod"]), ("team", ["core", "data"])],
      f"got {both}")

# A dimension filter is not a tag filter and must not be silently treated as one.
dim = main._budget_tags({"dimensions": {"name": "ResourceGroupName", "values": ["rg1"]}})
check("a dimension filter yields no tag clauses, rather than a wrong one", dim == [],
      f"got {dim}")

print("\nthe query, against a real warehouse")

tmp = Path(tempfile.mkdtemp()) / "costs.duckdb"
wh = Warehouse(db_path=tmp)
idx = {c: i for i, c in enumerate(COLUMNS)}


def row(day, cost, sub="sub-a", tags=""):
    r = [0.0 if c in NUMERIC else "" for c in COLUMNS]
    r[idx["ChargePeriodStart"]] = day
    r[idx["BilledCost"]] = cost
    r[idx["CostInUsd"]] = cost
    r[idx["BillingCurrency"]] = "USD"
    r[idx["SubAccountId"]] = sub
    r[idx["ServiceFamily"]] = "Compute"
    r[idx["Tags"]] = tags
    return r


records = [
    row("2026-08-01", 10.0, tags='{"Contact":"support@ms.com","env":"prod"}'),
    row("2026-08-02", 20.0, tags='{"Contact":"support@ms.com"}'),
    row("2026-08-02", 5.0, tags='{"env":"dev"}'),
    row("2026-08-03", 100.0, tags=""),
    row("2026-07-15", 999.0, tags='{"Contact":"support@ms.com"}'),   # previous period
    row("2026-08-02", 777.0, sub="sub-b", tags='{"Contact":"support@ms.com"}'),
]
with wh.connect() as con:
    names = ",".join(f'"{c}"' for c in COLUMNS)
    con.executemany(f"INSERT INTO costs ({names}) VALUES ({','.join('?' * len(COLUMNS))})",
                    records)

t = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31")
check("the period is respected — July is not in an August budget",
      t["total"] == 135.0, f"got {t['total']}")
check("and another subscription's spend is not either", 777.0 not in t["values"])
check("days come back in order", t["labels"] == ["2026-08-01", "2026-08-02", "2026-08-03"],
      f"got {t['labels']}")
check("the cumulative line only ever climbs",
      t["cumulative"] == [10.0, 35.0, 135.0], f"got {t['cumulative']}")

# The case that matters: this estate has both `Contact` and `contact` as tag keys, and Azure
# treats them as one when filtering a budget.
tagged = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31",
                         tags=[("contact", ["support@ms.com"])])
check("a tag filter matches regardless of key case",
      tagged["total"] == 30.0, f"got {tagged['total']}")
check("and excludes rows without the tag", 100.0 not in tagged["values"])

value_case = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31",
                             tags=[("Contact", ["SUPPORT@MS.COM"])])
check("and regardless of value case", value_case["total"] == 30.0,
      f"got {value_case['total']}")

any_value = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31", tags=[("env", [])])
check("a key with no values means 'has this tag at all'",
      any_value["total"] == 15.0, f"got {any_value['total']}")

missing = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31",
                          tags=[("nothing", ["here"])])
check("a filter that matches nothing returns an empty trend, not an error",
      missing["total"] == 0.0 and missing["labels"] == [], f"got {missing}")

# Malformed tag JSON is real: some rows carry '' and some carry junk.
with wh.connect() as con:
    con.executemany(f"INSERT INTO costs ({names}) VALUES ({','.join('?' * len(COLUMNS))})",
                    [row("2026-08-04", 1.0, tags="not json at all")])
survives = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31",
                           tags=[("contact", ["support@ms.com"])])
check("unparseable tag JSON does not break the query",
      "error" not in survives and survives["total"] == 30.0, f"got {survives}")

# Injection: tag keys and values come from Azure, but they are still data.
inject = wh.budget_trend("sub-a", "2026-08-01", "2026-08-31",
                         tags=[("x'; DROP TABLE costs; --", ["y"])])
check("a quote in a tag name is a parameter, not SQL", "error" not in inject)
with wh.connect(read_only=True) as con:
    still = con.execute("SELECT count(*) FROM costs").fetchone()[0]
check("the table is still there afterwards", still == 7, f"got {still}")

print(f"\n  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

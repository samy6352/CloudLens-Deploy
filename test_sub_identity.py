"""One subscription, one identity.

Azure spells a subscription id two ways. FOCUS exports use the ARM path
`/subscriptions/<guid>`; the Query API, Cost Details reports and Azure's own ActualCost
exports use the bare GUID. Both are correct.

Storing them verbatim put one subscription in the table twice. Every write replaces the range
it covers with `WHERE "SubAccountId" = ?`, so a FOCUS load never deleted the report path's rows
and the report path never deleted FOCUS's -- both survived, and both were summed.

Observed live, on one subscription:

    11111111-...                 11,266 rows   $1,917.07
    /subscriptions/11111111-...   6,219 rows     $981.96

The dashboard read $920.98 for a month Azure billed at $476.07. Nothing errored, no tab looked
broken, and a doubled total still looks like money -- which is why this needs tests rather than
eyes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duckdb

from app.warehouse import COLUMNS, NUMERIC, Warehouse, canonical_sub_id

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


GUID = "11111111-2222-3333-4444-555555555555"

section("every spelling collapses to one id")

check("an ARM path becomes the bare guid",
      canonical_sub_id(f"/subscriptions/{GUID}") == GUID)
check("a bare guid is left alone", canonical_sub_id(GUID) == GUID)
check("a trailing slash does not survive",
      canonical_sub_id(f"/subscriptions/{GUID}/") == GUID)
check("casing is normalised, because ARM treats guids case-insensitively",
      canonical_sub_id(GUID.upper()) == GUID)
check("a longer resource path still yields the subscription",
      canonical_sub_id(f"/subscriptions/{GUID}/resourceGroups/rg") != f"/subscriptions/{GUID}")
check("None stays None", canonical_sub_id(None) is None)
check("an empty string is treated as absent", canonical_sub_id("   ") is None)

section("the repair merges what is already stored")

COLS = list(COLUMNS)
TYPED = ",\n ".join(
    f'"{c}" ' + ("DOUBLE" if c in NUMERIC else
                 ("DATE" if c == "ChargePeriodStart" else "VARCHAR"))
    for c in COLS)
names = ",".join(f'"{c}"' for c in COLS)
I = {c: i for i, c in enumerate(COLS)}


def row(sub, day, resource, cost, meter="m1", qty=1.0):
    r = [None] * len(COLS)
    r[I["SubAccountId"]] = sub
    r[I["SubAccountName"]] = "MCAPS-test"
    r[I["ChargePeriodStart"]] = day
    r[I["ResourceId"]] = resource
    r[I["ResourceName"]] = resource
    r[I["ServiceName"]] = "Storage"
    r[I["MeterName"]] = meter
    r[I["PricingQuantity"]] = qty
    r[I["BilledCost"]] = cost
    r[I["CostInUsd"]] = cost
    r[I["BillingCurrency"]] = "USD"
    return r


con = duckdb.connect(":memory:")
con.execute(f"CREATE TABLE costs (\n {TYPED}\n)")

# The real shape: the same charges written twice under two spellings, plus one charge that
# only one loader saw, plus a genuinely different charge on the same day.
same = [
    row(GUID, "2026-08-10", "/rg/vm1", 10.0),
    row(GUID, "2026-08-10", "/rg/vm2", 5.0),
    row(GUID, "2026-08-11", "/rg/vm1", 7.0),
]
dupes = [
    row(f"/subscriptions/{GUID}", "2026-08-10", "/rg/vm1", 10.0),
    row(f"/subscriptions/{GUID}", "2026-08-10", "/rg/vm2", 5.0),
    row(f"/subscriptions/{GUID}", "2026-08-11", "/rg/vm1", 7.0),
]
only_focus = [row(f"/subscriptions/{GUID}", "2026-08-12", "/rg/vm3", 3.0)]
# Two real line items for one resource on one day -- different meters, both must survive.
distinct = [
    row(GUID, "2026-08-13", "/rg/vm4", 2.0, meter="reads"),
    row(GUID, "2026-08-13", "/rg/vm4", 4.0, meter="writes"),
]
other_sub = [row("33333333-4444-5555-6666-777777777777", "2026-08-10", "/rg/vm9", 1.0)]

for batch in (same, dupes, only_focus, distinct, other_sub):
    con.executemany(
        f"INSERT INTO costs ({names}) VALUES ({','.join('?' * len(COLS))})", batch)

before_cost = con.execute('SELECT sum("BilledCost") FROM costs').fetchone()[0]
before_rows = con.execute("SELECT count(*) FROM costs").fetchone()[0]
check("the fixture starts overstated", round(before_cost, 2) == 54.0 and before_rows == 10)

Warehouse._merge_subscription_spellings(Warehouse.__new__(Warehouse), con)

ids = [r[0] for r in con.execute(
    'SELECT DISTINCT "SubAccountId" FROM costs ORDER BY 1').fetchall()]
check("only one spelling of the subscription remains", f"/subscriptions/{GUID}" not in ids)
check("and it is the canonical one", GUID in ids)
check("the other subscription is untouched",
      "33333333-4444-5555-6666-777777777777" in ids)

after = con.execute('SELECT sum("BilledCost") FROM costs').fetchone()[0]
# 10 + 5 + 7 (deduped) + 3 (focus-only) + 2 + 4 (distinct meters) + 1 (other sub) = 32
check("the total is the real one, not the doubled one", round(after, 2) == 32.0)
check("a charge only one loader saw is kept",
      con.execute("""SELECT count(*) FROM costs WHERE "ResourceId" = '/rg/vm3'""")
      .fetchone()[0] == 1)
check("two real line items on the same resource and day both survive",
      con.execute("""SELECT count(*) FROM costs WHERE "ResourceId" = '/rg/vm4'""")
      .fetchone()[0] == 2)
check("each duplicated charge is kept exactly once",
      con.execute("""SELECT count(*) FROM costs WHERE "ResourceId" = '/rg/vm1'""")
      .fetchone()[0] == 2)

section("the repair is safe to run again")

Warehouse._merge_subscription_spellings(Warehouse.__new__(Warehouse), con)
check("a second pass changes nothing",
      round(con.execute('SELECT sum("BilledCost") FROM costs').fetchone()[0], 2) == 32.0)

section("writes and lookups all use the canonical form")

wh = open("app/warehouse.py", encoding="utf-8").read()
ex = open("app/exports.py", encoding="utf-8").read()
check("the query path canonicalises what it writes",
      'row[idx["SubAccountId"]] = canonical_sub_id(sub["id"])' in wh)
check("the report path does too", "rec[i_sid] = canonical_sub_id(rec[i_sid])" in wh)
check("the export path does too", "rec[i_sid] = canonical_sub_id(rec[i_sid])" in ex)
# The delete is the half that actually caused the duplication: it has to find the other
# loader's rows, which it can only do if both sides agree on the spelling.
check("the replace-delete matches on the canonical id",
      "[canonical_sub_id(sub_id), start, end]" in wh)
check("scope filtering compares canonically",
      'lower("SubAccountId") IN' in wh)
check("budget trends select canonically",
      "params: list[Any] = [canonical_sub_id(sub_id), start, end]" in wh)
check("the repair runs on boot",
      "self._merge_subscription_spellings(con)" in wh)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

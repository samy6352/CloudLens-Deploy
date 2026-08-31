"""The raw cost export: the one endpoint that hands over the underlying rows.

Every other export is a summary. This one returns the warehouse itself, filtered — which makes
it the most direct way to get data out of CloudLens, and therefore the one most worth proving
cannot return a row the caller is not entitled to.

    python test_raw_export.py
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "true")

import csv
import io

from fastapi.testclient import TestClient

from app.main import app
from app.warehouse import warehouse

client = TestClient(app)

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def rows_of(response) -> list[dict]:
    return list(csv.DictReader(io.StringIO(response.text)))


with warehouse.reader(None) as r:
    subs = r.rows(
        'SELECT DISTINCT "SubAccountId" AS id, "SubAccountName" AS name '
        'FROM costs WHERE "SubAccountId" IS NOT NULL',
        20,
    )

print("\nit downloads as a file")
print("=" * 72)
res = client.get("/api/costs/raw.csv", params={"days": 30})
check("the endpoint exists", res.status_code == 200, f"status {res.status_code}")
check(
    "it is marked as an attachment",
    res.headers.get("content-disposition", "").startswith("attachment;"),
    res.headers.get("content-disposition", "<none>"),
)
check("it is served as CSV", "csv" in res.headers.get("content-type", ""),
      res.headers.get("content-type", "<none>"))
check("it is not cached", res.headers.get("cache-control") == "no-store")

all_rows = rows_of(res)
check("it returns rows", len(all_rows) > 0, f"{len(all_rows)} rows")
check(
    "the header carries the warehouse columns, not a summary",
    {"ChargePeriodStart", "BilledCost", "SubAccountId", "ServiceName"} <= set(all_rows[0].keys()),
    f"{len(all_rows[0])} columns",
)
check(
    "commitment attribution is present as columns, even when unpopulated",
    {"PricingModel", "BenefitName", "BenefitId"} <= set(all_rows[0].keys()),
)

print("\nscope is enforced, not suggested")
print("=" * 72)
if len(subs) < 2:
    print("  SKIP  needs at least two subscriptions in the warehouse")
else:
    one = subs[0]
    scoped = rows_of(client.get("/api/costs/raw.csv",
                                params={"days": 30, "scope": one["id"]}))
    check("a scoped download returns fewer rows", len(scoped) < len(all_rows),
          f"{len(scoped)} vs {len(all_rows)}")
    check(
        "and returns only that subscription — nothing leaks past the filter",
        {r["SubAccountId"] for r in scoped} == {one["id"]},
        f"{len({r['SubAccountId'] for r in scoped})} distinct subscription(s)",
    )

    two = subs[1]
    both = rows_of(client.get("/api/costs/raw.csv",
                              params={"days": 30, "scope": f"{one['id']},{two['id']}"}))
    check(
        "two subscriptions return more than one but no more than all",
        len(scoped) < len(both) <= len(all_rows),
        f"one={len(scoped)} two={len(both)} all={len(all_rows)}",
    )

check(
    "an unknown subscription id returns nothing rather than everything",
    len(rows_of(client.get("/api/costs/raw.csv",
                           params={"days": 30,
                                   "scope": "00000000-0000-0000-0000-000000000000"}))) == 0,
)
check(
    "a blank scope is treated as no scope rather than as an empty allow-list",
    len(rows_of(client.get("/api/costs/raw.csv", params={"days": 30, "scope": ""}))) == len(all_rows),
)

print("\nthe period is honoured and clamped")
print("=" * 72)
short = rows_of(client.get("/api/costs/raw.csv", params={"days": 7}))
check("a shorter period returns fewer rows", len(short) < len(all_rows),
      f"7d={len(short)} 30d={len(all_rows)}")
check("an absurd period is clamped rather than refused",
      client.get("/api/costs/raw.csv", params={"days": 99999}).status_code == 200)
check("a negative period is clamped too",
      client.get("/api/costs/raw.csv", params={"days": -5}).status_code == 200)

print("\nthe numbers agree with the dashboard")
print("=" * 72)
exec_view = client.get("/api/dashboard/executive", params={"days": 30}).json()
csv_total = sum(float(r["BilledCost"] or 0) for r in all_rows)
check(
    "the raw rows add up to the headline total",
    abs(csv_total - exec_view["kpis"]["total"]) < 1.0,
    f"csv={csv_total:.2f} dashboard={exec_view['kpis']['total']:.2f}",
)

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

"""Does currency conversion stay honest?

The danger with converted money is not arithmetic, it is authority: a figure that looks precise
but cannot be traced to a rate, a date and a source is worse than no conversion at all. Most of
these assert that the app refuses rather than guesses.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\azure-cost-agent")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import currency as C  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
    if ok:
        passed += 1
    else:
        failed += 1


print("=" * 72)
print("USD needs no exchange rate at all")

# The whole reason this is trustworthy: Azure puts the USD equivalent on the invoice.
sql = C.convert_sql("BilledCost", "USD")
check("USD converts via Azure's own CostInUsd", "CostInUsd" in sql, sql)
check("it falls back to the billed amount when that column is empty",
      "coalesce" in sql and "BilledCost" in sql)
check("USD needs no rate", C.rate_to("USD") == 1.0)

print("\n" + "=" * 72)
print("an unavailable currency is refused, not guessed")

C._cache.update(at=0, rates={}, date=None, source=None)
os.environ.pop("FX_RATES", None)
check("an unknown currency has no rate", C.rate_to("XYZ") is None)
check("and its SQL leaves the amount as billed",
      C.convert_sql("BilledCost", "XYZ") == '"BilledCost"',
      C.convert_sql("BilledCost", "XYZ"))
d = C.describe("XYZ")
check("the disclosure says figures stay as billed",
      d["converted"] is False and "stay as billed" in d["basis"], d["basis"][:60])
check("'as billed' is itself a valid choice",
      C.convert_sql("BilledCost", "") == '"BilledCost"')

print("\n" + "=" * 72)
print("a pinned rate overrides the market")

# An organisation booking at a fixed internal rate needs its reports to match its ledger.
os.environ["FX_RATES"] = '{"INR": 80.0}'
C._cache.update(at=9e9, rates={"INR": 95.54}, date="2026-08-27", source="ECB")
check("the pinned rate wins over the live one", C.rate_to("INR") == 80.0, str(C.rate_to("INR")))
d = C.describe("INR")
check("and says it was pinned, not fetched", "pinned by this deployment" in d["basis"],
      d["basis"][:70])
check("a pinned rate carries no date, because it does not move", d.get("as_of") is None)

os.environ.pop("FX_RATES", None)
check("without a pin the live rate is used", C.rate_to("INR") == 95.54, str(C.rate_to("INR")))
d = C.describe("INR")
check("a live rate is attributed", "ECB" in d["basis"], d["basis"][:60])
check("and dated", "2026-08-27" in d["basis"])
check("and warns that rates move", "move daily" in d["basis"])

print("\n" + "=" * 72)
print("the arithmetic")

sql = C.convert_sql("BilledCost", "INR")
check("conversion pivots on USD, not on the billed amount",
      "CostInUsd" in sql and "95.54" in sql, sql)
check("the rate multiplies rather than divides", "* 95.54" in sql)

print("\n" + "=" * 72)
print("a failed fetch keeps yesterday's rates")

async def stale():
    C._cache.update(at=0, rates={"INR": 95.54}, date="2026-08-27", source="ECB")
    real = C._fetch
    async def broken():
        return {"rates": {}, "date": None, "source": None}
    C._fetch = broken
    try:
        await C.refresh(force=True)
    finally:
        C._fetch = real
    return C._cache

cache = asyncio.run(stale())
check("a broken feed does not wipe the rate table", cache["rates"].get("INR") == 95.54,
      "conversion keeps working on the last good rates")
check("and the date still reflects when they were published", cache["date"] == "2026-08-27")

print("\n" + "=" * 72)
print("the live feed")

async def live():
    C._cache.update(at=0, rates={}, date=None, source=None)
    return await C.available()

try:
    a = asyncio.run(live())
    check("real rates are fetched", len(a["supported"]) > 1, f"{len(a['supported'])} currencies")
    check("INR is available", "INR" in a["supported"])
    check("AED is available despite the ECB not publishing it", "AED" in a["supported"],
          "needs the fallback source")
    check("the rates are plausible", 50 < (a["rates"].get("INR") or 0) < 150,
          f"INR={a['rates'].get('INR')}")
    check("the response is dated", bool(a["as_of"]), str(a["as_of"]))
    check("and names its source", bool(a["source"]), a["source"])
except Exception as exc:  # noqa: BLE001 - offline is not a test failure
    print(f"  SKIP live feed unreachable: {str(exc)[:60]}")

print("\n" + "=" * 72)
print("wiring")

import inspect  # noqa: E402

from app import main as appmain  # noqa: E402

routes = {getattr(r, "path", "") for r in appmain.app.routes}
check("GET /api/currency exists", "/api/currency" in routes)
check("rates are warmed at startup", "currency.refresh()" in inspect.getsource(appmain.lifespan))

dash = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
check("the picker only appears when there is a choice", "supported || []).length < 2" in dash)
check("the choice is part of the cache key", 'readStore("costCurrency"' in dash)

print("\n" + "=" * 72)
print("every figure on the page moves, not just the ones on the front tab")

# The bug this section exists to catch: the picker, the rate feed and the disclosure text were
# all built and all correct, and `?currency=INR` returned dollars, because no query actually
# applied the rate. Everything above passed while the feature did nothing. So: assert that each
# endpoint that shows money can be *asked* for a currency, and that the two mechanisms which do
# the converting actually change a number.

sig_needs = {
    "overview": "the header tiles",
    "dashboard_sections": "the tab bar totals",
    "dashboard_executive": "the opening view",
    "dashboard_section": "every drill-down",
    "dashboard_waste": "stale resources",
    "dashboard_tags": "cost by tag",
    "dashboard_commitments": "reservations and Spot",
    "dashboard_anomalies": "anomaly findings",
    "dashboard_shutdown": "schedule savings",
    "dashboard_rightsizing": "oversized VMs",
    "dashboard_advisor": "Advisor savings",
    "dashboard_rates": "commitment recommendations",
    "dashboard_esu": "extended security updates",
    "list_budgets": "budget limits and spend",
}
for name, what in sig_needs.items():
    fn = getattr(appmain, name, None)
    params = inspect.signature(fn).parameters if fn else {}
    check(f"{what} accept a display currency", "currency" in params, f"({name})")

# Mechanism 1: SQL. A warehouse-backed tab converts by rewriting the query.
inr = C.convert_sql("BilledCost", "INR")
if C.rate_to("INR"):
    check("the SQL for a non-USD currency multiplies by a rate",
          "*" in inr and inr != '"BilledCost"', inr)
else:
    print("  SKIP no INR rate available offline")

# Mechanism 2: the boundary. Azure-sourced tabs convert the response on the way out, and must
# move the money without touching anything else in it.
payload = {
    "total_cost": 100.0,
    "count": 12,                       # a count of resources, not money
    "percent_used": 87.5,              # a proportion
    "period_days": 30,
    "enabled": True,                   # bool is an int in Python
    "findings": [{"cost": 10.0, "name": "disk-1", "resources": 3}],
}
out = C.convert_money(payload, "INR")
rate = C.rate_to("INR")
if rate:
    check("money is converted at the boundary", out["total_cost"] == round(100.0 * rate, 2),
          f"{out['total_cost']}")
    check("nested money is converted too", out["findings"][0]["cost"] == round(10.0 * rate, 2))
    check("a count is left alone", out["count"] == 12)
    check("a percentage is left alone", out["percent_used"] == 87.5)
    check("a day window is left alone", out["period_days"] == 30)
    check("a flag is not multiplied", out["enabled"] is True)
    check("the original is not mutated", payload["total_cost"] == 100.0)
else:
    print("  SKIP no INR rate available offline")

check("USD is a no-op rather than a rebuild", C.convert_money(payload, "USD") is payload)
check("as-billed is a no-op", C.convert_money(payload, "") is payload)
check("an unavailable currency leaves figures alone",
      C.convert_money(payload, "ZZZ") is payload)

# Anomaly floors are money, so they have to travel with the rate. A $15 floor left at 15 while
# the figures became rupees would flag a different set of days depending on the currency someone
# happened to be viewing — the events did not change, only the units.
from app import anomalies as A  # noqa: E402

check("the anomaly detector's money floor is adjustable",
      "min_baseline" in inspect.signature(A.detect).parameters)
check("analyse scales its floors by the same rate",
      "MIN_DELTA * scale" in inspect.getsource(A.analyse))

print("\n" + "=" * 72)
print("an exported file agrees with the screen it came from")

# The Export data tab showed dollars while the dashboard showed rupees, because the options
# endpoint never took a currency — and, worse, the downloaded file was in dollars too. A report
# that disagrees with the page it was exported from is the one output nobody can sanity-check,
# because it gets pasted into a deck and read a week later.
from app import report as R  # noqa: E402

for fn, what in (("collect", "gathering the data"), ("build", "writing the file")):
    check(f"{what} takes a currency", "currency" in inspect.signature(getattr(R, fn)).parameters)
check("the options endpoint takes one too",
      "currency" in inspect.signature(appmain.report_options).parameters)
check("the download link takes one",
      "currency" in inspect.signature(appmain.download_report).parameters)
check("and so does the POST body", "currency" in appmain.ReportRequest.model_fields)
check("live datasets in a report are converted as well",
      "fx.convert_money(result, body.currency)" in inspect.getsource(appmain._report_response))

js = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
check("the report links carry the display currency",
      'p.set("currency", cur)' in js)
# Raw rows are the billed record. Converting them would make the export disagree with the
# invoice it exists to reconcile against, which is the opposite of helpful.
check("raw cost rows are deliberately left as billed", 'raw.delete("currency")' in js)

# Six of the ten offered currencies used to render with no symbol at all, because three
# builders each carried their own four-entry table.
for code in C.OFFERED:
    check(f"{code} has a symbol in exported files", bool(C.symbol(code)), C.symbol(code))
check("an unknown code falls back to the code, not to nothing", C.symbol("ZZZ") == "ZZZ ")
check("as-billed still has no symbol", C.symbol("") == "")
check("there is only one symbol table now",
      R.__dict__.get("SYMBOLS") is None
      and 'symbol = {"USD": "$"' not in (ROOT / "app" / "report.py").read_text(encoding="utf-8"))

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

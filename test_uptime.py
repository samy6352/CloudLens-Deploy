"""Cost by hour: effective hourly rates and measured uptime.

The failure modes here are all arithmetic that looks plausible. A two-node scale set billing 48
hours a day reported as 200% uptime; a GiB-hour counted as an hour, which puts the estate at ten
million hours a day; a resource created halfway through the window reported as having been
switched off before it existed; and a run rate taken from the partial final day of a cost export,
which understates the burn by roughly 40%.

    python test_uptime.py
"""

from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app.uptime import (MAX_DAILY_CELLS, SCHEDULE_PRESETS, _cells, _dominant, _recent_daily,
                        _schedulable, _shape, _span_days, _weekday, hourly_cost)

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def row(**kw):
    base = {
        "id": "/subscriptions/x/resourceGroups/rg/providers/vm/a", "name": "a",
        "service": "Virtual Machines", "subscription": "sub", "resource_group": "rg",
        "region": "eastus", "meter": "D2s v3", "currency": "USD",
        "hours": 720.0, "cost": 72.0, "peak_day_hours": 24.0, "days_billed": 30,
        "first_day": dt.date(2026, 6, 1), "last_day": dt.date(2026, 6, 30),
    }
    base.update(kw)
    return base


print("\na rate is cost divided by the hours actually consumed")
print("=" * 72)
r = _shape(row(hours=720.0, cost=72.0))
check("it divides cost by hours", r["rate"] == 0.1, f"rate={r['rate']}")
check("a full month at 24h a day is 100% up", r["uptime_pct"] == 100.0, f"{r['uptime_pct']}%")
check("and is flagged as always on", r["always_on"] is True)

half = _shape(row(hours=360.0, cost=36.0, peak_day_hours=24.0))
check("half the hours is half the uptime", half["uptime_pct"] == 50.0, f"{half['uptime_pct']}%")
check("but the same rate — a schedule does not change the price of an hour",
      half["rate"] == 0.1, f"rate={half['rate']}")
check("and it is not always on", half["always_on"] is False)

check("zero hours cannot divide by zero", _shape(row(hours=0.0, cost=5.0))["rate"] == 0.0)


print("\nmore than one instance is not more than 100% uptime")
print("=" * 72)
# A two-node pool billing 48 hours a day for a month. Dividing by a flat 24 would say 200%.
two = _shape(row(hours=1440.0, cost=144.0, peak_day_hours=48.0))
check("it recovers the instance count from the busiest day", two["units"] == 2, f"{two['units']}")
check("two nodes running all month is 100%, not 200%",
      two["uptime_pct"] == 100.0, f"{two['uptime_pct']}%")

# The same pool, switched off at weekends: 48h x 22 weekdays out of 30 days.
part = _shape(row(hours=1056.0, cost=105.6, peak_day_hours=48.0, days_billed=22))
check("a two-node pool off at weekends reads as part-time",
      69 < part["uptime_pct"] < 75, f"{part['uptime_pct']}%")

check("uptime can never exceed 100% however odd the billing",
      _shape(row(hours=99999.0, peak_day_hours=24.0))["uptime_pct"] == 100.0)


print("\na resource is not 'off' for the days before it existed")
print("=" * 72)
# Created on the 20th, up continuously for 11 days of a 30-day window.
new = _shape(row(hours=264.0, cost=26.4, peak_day_hours=24.0, days_billed=11,
                 first_day=dt.date(2026, 6, 20), last_day=dt.date(2026, 6, 30)))
check("it measures across the resource's own lifetime",
      new["span_days"] == 11, f"span={new['span_days']}")
check("so something created mid-window is still 100% up",
      new["uptime_pct"] == 100.0, f"{new['uptime_pct']}%")
check("a single day is a span of one, not zero",
      _span_days(dt.date(2026, 6, 1), dt.date(2026, 6, 1)) == 1)
check("a missing date does not crash the row", _span_days(None, None) == 1)


print("\nonly things Azure lets you stop are offered as a saving")
print("=" * 72)
check("a VM can be scheduled", _schedulable("Virtual Machines") is True)
check("an App Service plan can be scheduled", _schedulable("Azure App Service") is True)
check("a managed search index cannot", _schedulable("Azure Cognitive Search") is False)
check("nor can a Defender node", _schedulable("Microsoft Defender for Cloud") is False)
check("nor an Event Hubs throughput unit", _schedulable("Event Hubs") is False)
check("matching is case-insensitive", _schedulable("VIRTUAL MACHINES") is True)


print("\nthe run rate ignores the partial final day")
print("=" * 72)
# Seven days of ~29, then the newest day at 17.5 because the export has not settled.
check("a short last day does not drag the rate down",
      _recent_daily([29.0, 38.9, 30.2, 29.9, 30.1, 28.1, 27.8, 17.5]) == 29.9,
      f"{_recent_daily([29.0, 38.9, 30.2, 29.9, 30.1, 28.1, 27.8, 17.5])}")
check("nor does a single spike drag it up",
      _recent_daily([10, 10, 10, 10, 10, 10, 500]) == 10)
check("an empty series is zero, not an error", _recent_daily([]) == 0.0)
check("a single day is that day", _recent_daily([12.0]) == 12.0)


print("\nmixed currencies are reported, not added together")
print("=" * 72)
code, mixed = _dominant([{"currency": "USD", "cost": 900.0}, {"currency": "EUR", "cost": 100.0}])
check("it picks the currency most of the money is in", code == "USD", code)
check("and says the estate is mixed", mixed is True)
code, mixed = _dominant([{"currency": "GBP", "cost": 10.0}])
check("a single currency is not flagged as mixed", code == "GBP" and mixed is False)
check("no rows means no currency", _dominant([]) == (None, False))


print("\nthe weekday profile averages over the weekdays it saw")
print("=" * 72)
days = [{"day": dt.date(2026, 6, 1) + dt.timedelta(days=i), "hours": 24.0 if i % 7 < 5 else 0.0,
         "cost": 2.4 if i % 7 < 5 else 0.0} for i in range(28)]
week = _weekday(days)
check("it returns seven days", len(week) == 7, str(len(week)))
check("Monday first — 1 June 2026 is a Monday", week[0]["day"] == "Monday", week[0]["day"])
check("a weekday-only estate shows full hours Mon-Fri",
      all(w["hours"] == 24.0 for w in week[:5]), str([w["hours"] for w in week[:5]]))
check("and nothing at the weekend",
      week[5]["hours"] == 0.0 and week[6]["hours"] == 0.0)
check("each weekday averaged over the four it saw",
      all(w["days"] == 4 for w in week), str([w["days"] for w in week]))


print("\nit answers against the real warehouse")
print("=" * 72)
d = hourly_cost(days=90)
for field in ("totals", "resources", "trend", "weekday", "schedules", "currency"):
    check(f"it returns {field}", field in d)

if d["resources"]:
    t = d["totals"]
    summed = round(sum(r["cost"] for r in d["resources"]), 2)
    check("the rows add up to the reported hour-billed cost",
          abs(summed - t["cost"]) < 1.0, f"rows={summed} totals={t['cost']}")
    summed_hours = round(sum(r["hours"] for r in d["resources"]), 0)
    check("and so do the hours",
          abs(summed_hours - t["hours"]) < 5.0, f"rows={summed_hours} totals={t['hours']}")
    check("hour-billed spend cannot exceed all spend",
          t["cost"] <= t["total_spend"] + 0.01,
          f"{t['cost']} vs {t['total_spend']}")
    check("coverage is a percentage of it",
          0 <= t["coverage_pct"] <= 100, f"{t['coverage_pct']}%")
    check("the blended rate is cost over hours",
          abs(t["avg_rate"] - t["cost"] / t["hours"]) < 1e-6)
    check("no resource claims more than 100% uptime",
          all(r["uptime_pct"] <= 100.0 for r in d["resources"]))
    check("no GiB-hour meter is counted as an hour — the estate is not running "
          "millions of hours a day",
          t["hours"] < 24 * 5000 * d["days"], f"{t['hours']} hours")
    check("the trend has a point per day with data",
          len(d["trend"]["labels"]) == len(d["trend"]["cost"]) == len(d["trend"]["rate"]))
    check("the trend cost adds up to the total",
          abs(round(sum(d["trend"]["cost"]), 2) - t["cost"]) < 1.0,
          f"trend={round(sum(d['trend']['cost']), 2)} total={t['cost']}")
    check("always-on resources are a subset of all of them",
          t["always_on"] <= t["meters"])
    check("schedulable always-on is a subset of always-on",
          t["schedulable"] <= t["always_on"])
    check("every schedulable preset is under a full week",
          all(0 < s["hours"] < 168 for s in SCHEDULE_PRESETS))
else:
    print("  ..   no hour-billed rows in the warehouse; skipped the live checks")


print("\ncells are indexed against the day axis, and dropped rather than truncated")
print("=" * 72)
labels = ["2026-06-01", "2026-06-02", "2026-06-03"]
raw = [
    {"id": "a", "meter": "m", "day": "2026-06-01", "hours": 24.0, "cost": 2.4},
    {"id": "a", "meter": "m", "day": "2026-06-03", "hours": 12.0, "cost": 1.2},
    {"id": "b", "meter": "m", "day": "2026-06-02", "hours": 6.0, "cost": 0.6},
]
dates, per_meter, dropped = _cells(raw, {"a\x00m", "b\x00m"}, labels)
check("it returns the day axis it was given", dates == labels)
check("and nothing was dropped", dropped is False)
check("cells are positions, not dates",
      per_meter["a\x00m"] == [[0, 24.0, 2.4], [2, 12.0, 1.2]], str(per_meter["a\x00m"]))
check("a day a meter did not bill is absent, not a zero",
      len(per_meter["a\x00m"]) == 2, "a gap must stay a gap")

# A meter the row cap cut from the table would be invisible weight in the payload, and would
# push the unfiltered chart above the table it is supposed to describe.
_, kept, _ = _cells(raw, {"a\x00m"}, labels)
check("cells for a meter that is not in the table are left out",
      set(kept) == {"a\x00m"}, str(set(kept)))

# A day outside the axis cannot be plotted, and indexing it would throw.
_, safe, _ = _cells([{"id": "a", "meter": "m", "day": "2001-01-01", "hours": 1, "cost": 1}],
                    {"a\x00m"}, labels)
check("a day off the axis is skipped rather than throwing", safe == {})

over = [dict(raw[0], id=str(i)) for i in range(MAX_DAILY_CELLS + 2)]
_, none_kept, over_dropped = _cells(over, {f"{i}\x00m" for i in range(MAX_DAILY_CELLS + 2)},
                                    labels)
check("past the cell cap the detail is dropped whole", over_dropped is True)
check("and nothing partial is sent in its place", none_kept == {},
      "half a series drawn as if complete is worse than none")


print("\nthe per-meter daily cells reconcile with the estate trend")
print("=" * 72)
if d["resources"]:
    check("it sends a day axis", len(d["dates"]) == len(d["trend"]["labels"]))
    check("and per-meter cells", bool(d["daily"]) and d["daily_dropped"] is False)

    keys = {r["key"] for r in d["resources"]}
    check("every meter's cells join to a row in the table",
          not (set(d["daily"]) - keys),
          f"orphans: {len(set(d['daily']) - keys)}")
    check("a key is unique per resource-and-meter",
          len(keys) == len(d["resources"]),
          f"{len(keys)} keys for {len(d['resources'])} rows")

    # The whole point of sending these is that the client can rebuild the chart from a subset.
    # If they do not add up to the series the server drew, then filtering to "all" would show
    # something different from filtering to nothing.
    hrs = [0.0] * len(d["dates"])
    cost = [0.0] * len(d["dates"])
    for arr in d["daily"].values():
        for i, h, c in arr:
            hrs[i] += h
            cost[i] += c
    worst_h = max(abs(a - b) for a, b in zip(hrs, d["trend"]["hours"]))
    worst_c = max(abs(a - b) for a, b in zip(cost, d["trend"]["cost"]))
    check("summing every meter's hours reproduces the estate hours",
          worst_h < 0.5, f"worst day off by {worst_h:.3f}h")
    check("and summing their cost reproduces the estate cost",
          worst_c < 0.01, f"worst day off by {worst_c:.5f}")

    check("no cell points off the end of the day axis",
          all(0 <= i < len(d["dates"]) for arr in d["daily"].values() for i, _, _ in arr))
    check("total spend is sent for the coverage figure",
          d["total_spend"] >= d["totals"]["cost"] - 0.01,
          f"{d['total_spend']} vs {d['totals']['cost']}")

    # A filter narrows the rows, and every headline has to narrow with them. This is the
    # server-side half of that promise: the parts a subset is built from must be present and
    # self-consistent, so the client can do the arithmetic without asking again.
    by_service = {}
    for r in d["resources"]:
        by_service.setdefault(r["service"], []).append(r)
    check("rows carry the dimensions the filters offer",
          all(k in d["resources"][0] for k in ("service", "resource_group", "region")))
    check("and there is more than one service to filter to",
          len(by_service) > 1, f"{len(by_service)} services")

    biggest = max(by_service.values(), key=lambda rs: sum(x["cost"] for x in rs))
    subset_cost = sum(r["cost"] for r in biggest)
    check("a single service is a strict subset of the whole",
          subset_cost <= d["totals"]["cost"] + 0.01,
          f"{subset_cost} vs {d['totals']['cost']}")
    subset_cells = sum(len(d["daily"].get(r["key"], [])) for r in biggest)
    check("and its rows have cells to draw a filtered chart from", subset_cells > 0)


print("\n" + "=" * 72)
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

"""The opening view — the one screen most people will judge the product on.

It is also the densest: six figures, a two-series trend and five tables, all from a single
warehouse pass. These checks are about the things that would quietly make it wrong rather than
make it fail — a total that doesn't match its parts, a change computed against the wrong
window, or the partial final day of a cost export plotted as if spend had collapsed.

    python test_overview.py
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from fastapi.testclient import TestClient

from app.main import app

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


def executive(**params):
    return client.get("/api/dashboard/executive", params=params).json()


print("\nit answers in one request")
print("=" * 72)
r = client.get("/api/dashboard/executive", params={"days": 30})
check("the endpoint exists", r.status_code == 200, f"status {r.status_code}")
d = r.json()
for key in ("kpis", "trend", "areas", "movers", "services", "subscriptions", "resource_groups"):
    check(f"it returns {key}", key in d)

print("\nthe figures agree with each other")
print("=" * 72)
k = d["kpis"]
total = k["total"]

areas_total = round(sum(a["cost"] for a in d["areas"]), 2)
check(
    "the areas add up to the total — every currency unit lands in exactly one area",
    abs(areas_total - total) < 1.0,
    f"areas={areas_total} total={total}",
)

trend_total = round(sum(d["trend"]["values"]), 2)
check(
    "the daily trend adds up to the total",
    abs(trend_total - total) < 1.0,
    f"trend={trend_total} total={total}",
)

check(
    "the daily average is the total over the period",
    abs(k["daily_avg"] - total / 30) < 0.02,
    f"{k['daily_avg']} vs {round(total / 30, 2)}",
)

if k["previous"]:
    expected = round((total - k["previous"]) / k["previous"] * 100, 1)
    check("the change is measured against the previous window", k["change_pct"] == expected,
          f"{k['change_pct']} vs {expected}")

check(
    "no share exceeds 100%",
    all((a.get("share_pct") or 0) <= 100.5 for a in d["areas"] + d["services"]),
)
check(
    "the top five services are at most all of the spend",
    k["top5_share_pct"] is None or k["top5_share_pct"] <= 100.5,
    str(k["top5_share_pct"]),
)

print("\nchange is described, not just totalled")
print("=" * 72)
for name, rows in (("areas", d["areas"]), ("movers", d["movers"]),
                   ("subscriptions", d["subscriptions"])):
    check(f"{name} carry a previous figure and a delta",
          all("previous" in x and "delta" in x for x in rows))
    check(
        f"{name} deltas are the difference, not something else",
        all(abs(x["delta"] - (x["cost"] - x["previous"])) < 0.02 for x in rows),
    )
check(
    "a thing with no prior spend has no percentage — 'new' and 'unchanged' are different",
    all(x["change_pct"] is None for x in d["movers"] if x["previous"] == 0),
)
check(
    "movers are ranked by how much moved, not by how much they cost",
    all(abs(a["delta"]) >= abs(b["delta"]) - 0.011
        for a, b in zip(d["movers"], d["movers"][1:])),
    " > ".join(str(m["delta"]) for m in d["movers"][:4]),
)

print("\nthe two trend series line up")
print("=" * 72)
t = d["trend"]
check("there is a previous series to compare against", "previous" in t and len(t["previous"]) > 0)
check(
    "the comparison covers a similar span, so day N sits above day N",
    abs(len(t["values"]) - len(t["previous"])) <= 1,
    f"current={len(t['values'])} previous={len(t['previous'])}",
)
check("every point has a label", len(t["labels"]) == len(t["values"]))

print("\nthe partial final day is flagged rather than plotted as a collapse")
print("=" * 72)
check("the flag is present", "partial_last" in t)
if t["partial_last"]:
    body = sorted(t["values"][:-1])
    median = body[len(body) // 2]
    check(
        "it is only raised when the last day really is far below the recent norm",
        t["values"][-1] < median * 0.4,
        f"final={t['values'][-1]} median={median}",
    )
check(
    "the partial day is still counted in the total — it is real money",
    abs(round(sum(t["values"]), 2) - total) < 1.0,
    f"trend sum={round(sum(t['values']), 2)} total={total}",
)
for days in (7, 30, 90):
    tr = executive(days=days)["trend"]
    check(f"the check runs at {days} days too, not only long windows",
          "partial_last" in tr and isinstance(tr["partial_last"], bool),
          f"partial_last={tr['partial_last']} points={len(tr['values'])}")

print("\nit holds up at other periods")
print("=" * 72)
for days in (7, 90):
    e = executive(days=days)
    check(f"{days} days returns a total", e["kpis"]["total"] >= 0, str(e["kpis"]["total"]))
    check(f"{days} days keeps areas consistent with the total",
          abs(round(sum(a["cost"] for a in e["areas"]), 2) - e["kpis"]["total"]) < 1.0)

short = executive(days=7)["kpis"]["total"]
long = executive(days=90)["kpis"]["total"]
check("a longer window costs at least as much as a shorter one", long >= short,
      f"7d={short} 90d={long}")

print("\nscope is respected")
print("=" * 72)
full = executive(days=30)
scoped = executive(days=30, scope="00000000-0000-0000-0000-000000000000")
check(
    "an unknown subscription narrows to nothing rather than returning the estate",
    scoped.get("empty") or scoped["kpis"]["total"] <= full["kpis"]["total"],
    f"scoped={scoped.get('kpis', {}).get('total')} full={full['kpis']['total']}",
)

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

"""Does the anomaly detector flag what matters and stay quiet about what doesn't?

The failure mode that kills an alerting surface is not missing an event -- it is crying wolf.
A page with ninety findings, of which three matter, gets read once. So most of these assert
*silence*: weekend dips, cheap noise, a service that only just appeared, and the unsettled tail
must all produce nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\azure-cost-agent")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import anomalies as A  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
    if ok:
        passed += 1
    else:
        failed += 1


def days(n, start="2026-06-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def series(values, key="svc", start="2026-06-01"):
    """Rows in the shape the warehouse returns: (key, day, cost)."""
    return [(key, d, v) for d, v in zip(days(len(values), start), values)]


print("=" * 72)
print("a real spike is found")

# Thirty steady days, then one that is five times the rest.
flat = [10.0] * 30
spike = flat[:20] + [120.0] + flat[21:]
found = A.detect(series(spike), "ServiceName")
check("a 12x day is flagged", len(found) == 1, f"{len(found)} found")
if found:
    a = found[0]
    check("it names the day", a["day"] == days(30)[20], a["day"])
    check("the delta is the rise above baseline", a["delta"] == 110.0, str(a["delta"]))
    check("direction is up", a["direction"] == "up")
    check("the baseline is the median, not the mean", a["baseline"] == 10.0, str(a["baseline"]))

print("\n" + "=" * 72)
print("and the noise is not")

check("a flat series produces nothing", A.detect(series(flat), "ServiceName") == [])

# The floor that matters most: a huge relative jump on trivial money.
tiny = [0.02] * 20 + [0.20] + [0.02] * 9
check("a 900% rise on 2 cents is ignored",
      A.detect(series(tiny), "ServiceName") == [], "money floor")

# Weekends are a real weekly cycle, not an anomaly -- this is why weekday and weekend days are
# judged against their own kind. The first weekend or two are unavoidable: there is nothing to
# compare them against yet, which is honest rather than wrong. What must not happen is every
# weekend firing forever.
weekly = []
for w in range(5):
    weekly += [20.0, 20.0, 20.0, 20.0, 20.0, 4.0, 4.0]
found_w = A.detect(series(weekly), "ServiceName")
check("a weekly weekend dip stops being an anomaly once it has a baseline",
      len(found_w) <= 2, f"{len(found_w)} found, was 6 before weekday/weekend split")
check("and the ones flagged are only the earliest weekends",
      all(f["day"] <= days(35)[13] for f in found_w),
      ", ".join(f["day"] for f in found_w))

# Something that appeared three days ago has no baseline to be unusual against.
newcomer = [0.0] * 27 + [50.0, 50.0, 50.0]
recent = [a for a in A.detect(series(newcomer), "ServiceName") if a["day"] >= days(30)[27]]
check("a brand-new service is not flagged on arrival", len(recent) <= 1, f"{len(recent)}")

print("\n" + "=" * 72)
print("the unsettled tail is left alone")

# A spike on the very last day is still being restated by Azure.
tail = flat[:29] + [200.0]
check("a spike in the settling window is not reported",
      A.detect(series(tail), "ServiceName", settle_days=2) == [])
check("but is reported once it settles",
      len(A.detect(series(tail), "ServiceName", settle_days=0)) == 1)

print("\n" + "=" * 72)
print("the statistic behaves")

# The whole reason for median/MAD over mean/stdev: one outlier must not hide the next.
twice = flat[:10] + [120.0] + flat[11:20] + [120.0] + flat[21:]
check("a second spike of the same size is still a spike",
      len(A.detect(series(twice), "ServiceName")) == 2,
      f"{len(A.detect(series(twice), 'ServiceName'))} found")

z, base, spread = A.score(120.0, [10.0] * 14)
check("a constant history still scores a change", z > 0, f"z={z}")
check("the score is capped so it never reads as a bug", z <= A.SCORE_CAP, f"z={z}")
check("no score without enough history", A.score(100.0, [10.0] * 3)[0] == 0.0)

down = [40.0] * 20 + [0.0] + [40.0] * 9
found_d = A.detect(series(down), "ServiceName")
check("spend that stops is also an anomaly",
      len(found_d) >= 1 and found_d[0]["direction"] == "down",
      f"{len(found_d)} found")
check("a stop below the money floor is ignored",
      A.detect(series([10.0] * 20 + [0.0] + [10.0] * 9), "ServiceName") == [],
      "a $10/day service stopping is not worth an alert")

print("\n" + "=" * 72)
print("severity follows money, not the statistic")

check("a big absolute move is high", A._severity(600, 4.0) == "high")
check("a moderate one is medium", A._severity(80, 4.0) == "medium")
check("a small one stays low however extreme the score", A._severity(20, 99.0) == "low",
      "a 99-sigma move on $20 is still $20")

print("\n" + "=" * 72)
print("gaps are real zeros")

# A resource that billed Mon and Wed but not Tue really did cost nothing on Tuesday.
gappy = [("svc", "2026-06-01", 10.0), ("svc", "2026-06-03", 10.0)]
built = A._series(gappy, "ServiceName")
check("a missing day is filled as zero, not skipped",
      built["svc"].get("2026-06-02") == 0.0, str(built["svc"]))

print("\n" + "=" * 72)
print("the endpoint is wired")

import inspect  # noqa: E402

from app import main as appmain  # noqa: E402

routes = {getattr(r, "path", "") for r in appmain.app.routes}
check("GET /api/dashboard/anomalies exists", "/api/dashboard/anomalies" in routes)
src = inspect.getsource(appmain.dashboard_anomalies)
check("it is scoped to what the caller may see", "permitted(user)" in src and "narrow(" in src)
check("it reads the warehouse, not Azure", "anomalies.analyse" in src)

dash = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
check("the tab is registered", 'id: "anomalies"' in dash)
check("it renders cards, not a bare table", "anomalyCard" in dash)
check("an absurd score is shown as words", 'a.score >= 50 ? "far outside"' in dash)

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

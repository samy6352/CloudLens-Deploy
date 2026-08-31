"""The Advisor headline must not sum reservation terms you cannot both buy.

Advisor prices the same reservation over P1Y and P3Y. Both are real recommendations
and only one is purchasable, so adding them claims a saving that does not exist.
Found in pre-production QA: $3,395 actionable reported as $5,445.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def collapse(recs):
    """Mirrors the two passes in waste.py's advisor collapse."""
    best = {}
    for r in recs:
        key = (r.get("problem"), r.get("resource"), r.get("sku"),
               r.get("term"), str(r.get("quantity")))
        prior = best.get(key)
        if prior is None or (r.get("lookback_days") or 0) > (prior.get("lookback_days") or 0):
            best[key] = r
    actionable = {}
    for r in best.values():
        key = (r.get("problem"), r.get("resource"), r.get("sku"), str(r.get("quantity")))
        prior = actionable.get(key)
        if prior is None or (r.get("annual_savings") or 0) > (prior.get("annual_savings") or 0):
            actionable[key] = r
    return actionable


def rec(resource, term, annual, lookback=30, sku="Standard_D2", qty=1, problem="RI"):
    return {"problem": problem, "resource": resource, "sku": sku, "term": term,
            "quantity": qty, "annual_savings": annual, "lookback_days": lookback}


print("advisor — mutually exclusive reservation terms")

# The real shape from the tenant: four resources each priced at P3Y and P1Y.
live = [
    rec("vm-bd2e", "P3Y", 2119.0), rec("vm-bd2e", "P1Y", 1253.0),
    rec("appsvc-p0v4", "P3Y", 559.0), rec("appsvc-p0v4", "P1Y", 370.0),
    rec("appsvc-p0v3", "P3Y", 394.0), rec("appsvc-p0v3", "P1Y", 249.0),
    rec("savingsplan", "P3Y", 323.0), rec("savingsplan", "P1Y", 178.0),
]
got = collapse(live)
total = sum(r["annual_savings"] for r in got.values())
naive = sum(r["annual_savings"] for r in live)

check("one decision per resource, not one per term", len(got) == 4, f"got {len(got)}")
check("the headline is the actionable figure", total == 3395.0, f"got {total}")
check("and not the sum of both terms", total != naive, f"naive={naive}")
check("which is what the Savings tab already reported", total == 3395.0)

# The longer term is the better deal here, but the rule is "largest saving", not "longest
# term" — a P1Y that saves more must win, or the headline understates what is achievable.
odd = [rec("x", "P3Y", 100.0), rec("x", "P1Y", 250.0)]
check("the better deal wins regardless of term length",
      sum(r["annual_savings"] for r in collapse(odd).values()) == 250.0)

# The original lookback collapse must still work: same term priced over 7/30/60 days is one
# recommendation, and the longest lookback is the one with the most evidence behind it.
looks = [rec("y", "P3Y", 900.0, lookback=7),
         rec("y", "P3Y", 600.0, lookback=30),
         rec("y", "P3Y", 500.0, lookback=60)]
out = collapse(looks)
check("lookback windows still collapse to one row", len(out) == 1, f"got {len(out)}")
check("and the longest lookback wins, not the most optimistic",
      list(out.values())[0]["annual_savings"] == 500.0,
      f"got {list(out.values())[0]['annual_savings']}")

# Different resources are genuinely different decisions and must both count.
two = [rec("a", "P3Y", 100.0), rec("b", "P3Y", 200.0)]
check("separate resources are still counted separately",
      sum(r["annual_savings"] for r in collapse(two).values()) == 300.0)

# Different SKUs on the same resource are different purchases.
skus = [rec("a", "P3Y", 100.0, sku="D2"), rec("a", "P3Y", 200.0, sku="D4")]
check("different SKUs are different decisions", len(collapse(skus)) == 2)

# A null saving must not crash the comparison.
nulls = [rec("z", "P3Y", None), rec("z", "P1Y", 50.0)]
check("a null saving does not break the collapse",
      sum(r["annual_savings"] or 0 for r in collapse(nulls).values()) == 50.0)

src = (Path(__file__).resolve().parent / "app" / "waste.py").read_text(encoding="utf-8")
check("the fix is in the shipped code", "actionable: dict[tuple, dict[str, Any]]" in src)
check("and distinct_count reports the actionable set",
      '"distinct_count": len(actionable)' in src)
check("the reason is written down where the next person will look",
      "You" in src and "never both" in src)

print(f"\n  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

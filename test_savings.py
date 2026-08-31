"""The merge has to count each resource once.

Every figure in here is taken from the live estate on 2026-08-29, because the bug being guarded
against is arithmetic on real overlapping data rather than anything a synthetic fixture would
show. Three of those numbers matter most:

  * Advisor returned 16 cost recommendations that were 5 distinct problems.
  * $15.82 of deallocated VM spend appeared in both the orphan scan and the utilisation scan.
  * Rate optimization re-read Advisor, so 4 of its problem strings were already on the Advisor tab.

A merge that gets any of those wrong produces a savings total nobody can bank, which is worse
than the six separate tabs it replaces — at least those were honestly disconnected.
"""

from __future__ import annotations

import sys

from app.savings import (
    CATEGORIES,
    Opportunity,
    _from_advisor,
    _from_rightsizing,
    _from_waste,
    _Index,
    _norm_id,
    _rank,
    _res_key,
)

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")


def head(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}")


# --------------------------------------------------------------- real fixtures

# The three deallocated VMs, as the orphan scan reports them.
WASTE = {
    "currency": "USD",
    "findings": [
        {
            "category": "deallocated_vms",
            "title": "Deallocated VMs still holding disks",
            "why": "No compute charge, but their OS and data disks are still billed.",
            "count": 3, "cost": 15.82,
            "items": [
                {"name": "cp-fw-vmss_a4991664", "resource_group": "cp-nva-test",
                 "location": "centralindia", "detail": "Standard_D8as_v5, deallocated",
                 "cost": 6.89, "currency": "USD",
                 "id": "/subscriptions/33333333-4444-5555-6666-777777777777/resourceGroups/"
                       "cp-nva-test/providers/Microsoft.Compute/virtualMachines/cp-fw-vmss_a4991664"},
                {"name": "demovm1", "resource_group": "costanomaly",
                 "location": "centralindia", "detail": "Standard_D2s_v3, deallocated",
                 "cost": 5.92, "currency": "USD",
                 "id": "/subscriptions/33333333-4444-5555-6666-777777777777/resourceGroups/"
                       "CostAnomaly/providers/Microsoft.Compute/virtualMachines/demovm1"},
                {"name": "cp-fw-vmss_88a76fd5", "resource_group": "cp-nva-test",
                 "location": "centralindia", "detail": "Standard_D8as_v5, deallocated",
                 "cost": 3.01, "currency": "USD",
                 "id": "/subscriptions/33333333-4444-5555-6666-777777777777/resourceGroups/"
                       "cp-nva-test/providers/Microsoft.Compute/virtualMachines/cp-fw-vmss_88a76fd5"},
            ],
        },
        {
            "category": "unassociated_public_ips",
            "title": "Unassociated public IP addresses",
            "why": "A reserved standard IP is billed whether or not anything answers on it.",
            "count": 15, "cost": 42.02,
            "items": [{"name": f"pip{i}", "resource_group": "tub", "location": "westus",
                       "detail": "Standard, Static", "cost": 3.88, "currency": "USD",
                       "id": f"/subscriptions/e3d/resourceGroups/TUB/providers/"
                             f"Microsoft.Network/publicIPAddresses/pip{i}"} for i in range(15)],
        },
    ],
}

# The same three machines, as the utilisation scan reports them. Note: no ARM id at all, and the
# resource-group case differs from the orphan scan's — both are why the match is not trivial.
RIGHTSIZING = {
    "cpu_threshold": 5, "currency": "USD",
    "vms": [
        {"name": "demovm1", "resource_group": "CostAnomaly", "size": "Standard_D2s_v3",
         "location": "centralindia", "state": "deallocated", "cpu_avg": None,
         "cpu_peak": None, "days_with_data": 0, "cost": 5.92, "currency": "USD"},
        {"name": "cp-fw-vmss_88a76fd5", "resource_group": "cp-nva-test",
         "size": "Standard_D8as_v5", "location": "centralindia", "state": "deallocated",
         "cpu_avg": None, "cpu_peak": None, "days_with_data": 0, "cost": 3.01,
         "currency": "USD"},
        {"name": "cp-fw-vmss_a4991664", "resource_group": "cp-nva-test",
         "size": "Standard_D8as_v5", "location": "centralindia", "state": "deallocated",
         "cpu_avg": None, "cpu_peak": None, "days_with_data": 0, "cost": 6.89,
         "currency": "USD"},
        {"name": "TUB-VM", "resource_group": "tub", "size": "Standard_D2s_v3",
         "location": "westus", "state": "deallocating", "cpu_avg": None, "cpu_peak": None,
         "days_with_data": 0, "cost": 0.0, "currency": "USD"},
    ],
}

# Advisor's six App Service rows, exactly as Azure returns them: two real terms, each priced
# over three lookback windows. Note the shortest lookback is the most optimistic in both cases,
# which is why "take the largest" is the wrong rule.
ADVISOR = [
    {"problem": "Consider App Service reserved instance to save over the on-demand costs",
     "solution": "…", "impact": "High", "resource": "33333333-4444-5555-6666-777777777777",
     "annual_savings": s, "currency": "USD", "sku": "Azure_App_Service_Premium_v4_Plan_P0v4",
     "term": term, "quantity": "1", "region": "centralindia", "scope": "Single",
     "lookback_days": lb}
    for s, term, lb in [
        (563, "P3Y", 7), (558, "P3Y", 30), (558, "P3Y", 60),
        (374, "P1Y", 7), (370, "P1Y", 30), (370, "P1Y", 60),
    ]
]
ADVISOR += [
    {"problem": "Consider purchasing a savings plan to unlock lower prices",
     "solution": "…", "impact": "High", "resource": "22222222-3333-4444-5555-666666666666",
     "annual_savings": 954, "currency": "USD", "sku": "Compute", "term": "P1Y",
     "quantity": "1", "lookback_days": 30},
    {"problem": "Consider purchasing a savings plan to unlock lower prices",
     "solution": "…", "impact": "High", "resource": "22222222-3333-4444-5555-666666666666",
     "annual_savings": 957, "currency": "USD", "sku": "Compute", "term": "P3Y",
     "quantity": "1", "lookback_days": 30},
]


# ------------------------------------------------------------------- the tests

head("a resource named by two scans is one opportunity")

index = _Index()
waste_opps = _from_waste(WASTE, index, "USD")
rs_opps = _from_rightsizing(RIGHTSIZING, index, "USD")

check("the orphan scan produces one row per kind, not per resource",
      len(waste_opps) == 2, f"{len(waste_opps)} rows for 18 resources")

check("the utilisation scan adds no new rows for VMs already found",
      len(rs_opps) == 0, f"added {len(rs_opps)}")

vm_row = next(o for o in waste_opps if o.category == "orphaned" and "Deallocated" in o.title)
check("it attaches itself to the existing row instead",
      {s["source"] for s in vm_row.sources} == {"waste", "rightsizing"},
      str([s["source"] for s in vm_row.sources]))

check("and the money is not added twice",
      vm_row.window == 15.82, f"{vm_row.window} (must stay 15.82, not 31.64)")

check("the row is marked as corroborated", vm_row.corroborated)
check("which raises its confidence", vm_row.confidence == "high", vm_row.confidence)

# Three VMs from one scan attaching to one row must read as three, not as the first of them.
util = next(s for s in vm_row.sources if s["source"] == "rightsizing")
check("a source that matched three resources says so",
      util["matches"] == 3, f"matches={util['matches']}")
check("and totals them, so it does not appear to contradict the row",
      abs(util["window"] - 15.82) < 0.01, f"{util['window']} against row {vm_row.window}")

# The case difference is the whole reason this can silently fail.
check("matching survives Azure's inconsistent resource-group casing",
      _norm_id("/subs/X/resourceGroups/CostAnomaly/x")
      == _norm_id("/subs/x/resourcegroups/costanomaly/x"))
check("and works from name plus group when no id is sent",
      _res_key("demovm1", "CostAnomaly", "") == _res_key("DemoVM1", "costanomaly", ""))


head("a busy machine is not an opportunity")

# TUB-VM has no CPU data and is deallocating; it is in neither waste finding, so if it were
# treated as idle it would appear as a row worth $0.
idle_rows = [o for o in rs_opps if o.resource == "TUB-VM"]
check("a VM with no cost and no CPU signal does not become a row",
      not idle_rows or all(o.window > 0 for o in idle_rows))


head("Advisor repeats itself in two different ways")

adv = _from_advisor(ADVISOR, _Index(), "USD", 90, "advisor")
check("eight rows describing two decisions collapse to two",
      len(adv) == 2, f"{len(adv)} rows from {len(ADVISOR)}")

app_svc = next(o for o in adv if "App Service" in o.title)

# The two kinds of repetition, told apart. Terms are choices and survive as options; lookback
# windows are re-estimates of one choice and must not.
check("the two purchasable terms both survive as options",
      len(app_svc.options) == 2, f"{len(app_svc.options)} options")
check("the lookback re-estimates do not become options",
      app_svc.folded == 4, f"folded {app_svc.folded} (6 rows, 2 decisions)")
check("options are named by the decision, not the estimate",
      sorted(o["label"].split(" · ")[0] for o in app_svc.options) == ["1 year", "3 years"],
      str([o["label"] for o in app_svc.options]))

# The subtle one: max() would pick 563 (7-day lookback), the least-evidenced estimate.
check("the headline comes from the longest lookback, not the largest number",
      app_svc.annual == 558, f"{app_svc.annual} (max would be 563, from only 7 days)")
check("and it is not the sum of the rows",
      app_svc.annual < sum(r["annual_savings"] for r in ADVISOR[:6]),
      f"{app_svc.annual} vs sum 3183")
check("the spread across lookbacks is reported as the confidence range",
      any("depending on the lookback" in (o["detail"] or "") for o in app_svc.options),
      str([o["detail"] for o in app_svc.options]))

check("a reservation is categorised as a commitment",
      app_svc.category == "commitment", app_svc.category)
check("an annual estimate is scaled to the window for ranking",
      abs(app_svc.window - 558 * (90 / 365)) < 0.5, f"{app_svc.window}")
check("an Advisor-only row is not marked corroborated", not app_svc.corroborated)
check("and its confidence stays low", app_svc.confidence == "low", app_svc.confidence)

plan = next(o for o in adv if "savings plan" in o.title)
check("two genuinely different terms are kept as two options, nothing folded",
      len(plan.options) == 2 and plan.folded == 0,
      f"{len(plan.options)} options, folded {plan.folded}")


head("a subscription is a place, not a thing to act on")

# Advisor names the subscription as the resource for anything bought at subscription scope, so
# an App Service reservation and a savings plan in the same subscription arrive with identical
# resource fields. Treating that as an identity merged two unrelated recommendations and added
# their money — on the live estate it produced one row claiming 176.79 that was really 97.15 and
# 79.64 for two different things, plus a spurious "the scans priced this differently".
SAME_SUB = [
    {"problem": "Consider App Service reserved instance to save over the on-demand costs",
     "impact": "High", "resource": "22222222-3333-4444-5555-666666666666",
     "annual_savings": 394, "currency": "USD", "sku": "P0_v3", "term": "P3Y",
     "quantity": "1", "lookback_days": 30},
    {"problem": "Consider purchasing a savings plan to unlock lower prices",
     "impact": "High", "resource": "22222222-3333-4444-5555-666666666666",
     "annual_savings": 323, "currency": "USD", "sku": "Compute_Savings_Plan", "term": "P3Y",
     "quantity": "1", "lookback_days": 30},
]
same = _from_advisor(SAME_SUB, _Index(), "USD", 90, "advisor")
check("two recommendations in one subscription stay two rows",
      len(same) == 2, f"{len(same)} rows")
check("and their money is not added together",
      sorted(round(o.annual) for o in same) == [323, 394],
      str(sorted(round(o.annual) for o in same)))
check("neither is falsely reported as a disagreement between scans",
      all(o.disputed is None for o in same),
      str([o.disputed for o in same]))
check("nor as one source having found the thing twice",
      all(s["matches"] == 1 for o in same for s in o.sources))

# The other half of the same bug: a key built from three empty fields is "||", which is truthy,
# so every source with no resource fields would have matched every other.
check("a key with nothing in it is empty rather than '||'",
      _res_key(None, None, None) == "" and _res_key("", "", "") == "")
check("but a real name still keys", _res_key("vm1", "rg", "") != "")


head("the totals can be added up")

allopps = waste_opps + rs_opps + adv
ranked = _rank(allopps)
billed = sum(o.window for o in ranked
             if any(s["basis"] == "billed" for s in o.sources))
check("the billed total counts each resource exactly once",
      abs(billed - (15.82 + 42.02)) < 0.01, f"{billed:.2f}")

check("bills and projections are not mixed into one figure",
      billed != sum(o.window for o in ranked))

check("ranking puts the largest recoverable amount first",
      ranked[0].window == max(o.window for o in ranked))


head("every category a row can carry is one the UI offers")

for o in ranked:
    check(f"{o.title[:38]!r} has a known category", o.category in CATEGORIES, o.category)


head("nothing claims money it cannot show")

for o in ranked:
    if o.window > 0:
        check(f"{o.title[:38]!r} says where its figure came from", bool(o.sources))

print(f"\n{'=' * 72}\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

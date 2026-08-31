"""Rate optimisation: paying less for the same resources, rather than using fewer of them.

The rest of CloudLens reduces *usage*. This reduces the *rate* — reservations and savings plans,
where you commit to a level of spend and Azure discounts it.

Everything here comes from **Azure Advisor**, read through Resource Graph, which is what the
built-in Cost Optimization workbook does. That choice matters more than it looks. The raw
Consumption `benefitRecommendations` API returns figures scoped to the whole *billing account*:
on this estate it reports $3.99M of eligible spend and $1.5M of savings, which is thousands of
other people's subscriptions. Presenting that to someone who can see one subscription would be
wrong by three orders of magnitude, and would leak aggregate billing data straight through the
RBAC boundary the rest of the app enforces.

Advisor's version of the same recommendation is scoped to the subscription and attributed to
the person asking. Smaller numbers, but they are *their* numbers.

Everything here is read-only.
"""
from __future__ import annotations

import logging
from typing import Any

from .cost import list_subscriptions
from .waste import _arg

log = logging.getLogger("cloudlens.rates")

# Advisor labels these by the text of the recommendation rather than a stable enum, so match on
# the shape of the problem statement. Kept here rather than inline so a wording change is one
# edit and shows up in a diff.
COMMITMENT_PATTERNS = (
    "savings plan",
    "reserved instance",
    "reservation",
)

ADVISOR_COST_QUERY = """
    advisorresources
    | where type =~ 'microsoft.advisor/recommendations'
    | where tostring(properties.category) == 'Cost'
    | extend props = properties, ext = properties.extendedProperties
    | project
        problem      = tostring(props.shortDescription.problem),
        solution     = tostring(props.shortDescription.solution),
        impact       = tostring(props.impact),
        impactedType = tostring(props.impactedField),
        impactedName = tostring(props.impactedValue),
        subscriptionId,
        resourceGroup,
        savings      = ext.savingsAmount,
        currency     = tostring(ext.savingsCurrency),
        annualSavings= ext.annualSavingsAmount,
        term         = tostring(ext.term),
        lookback     = tostring(ext.lookbackPeriod),
        skuName      = tostring(ext.displaySKU),
        quantity     = ext.displayQty,
        region       = tostring(ext.region),
        scopeHint    = tostring(ext.scope)
"""


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _is_commitment(problem: str) -> bool:
    low = (problem or "").lower()
    return any(p in low for p in COMMITMENT_PATTERNS)


def _lookback_days(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _best_per_option(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct decision, rather than one per Advisor lookback window.

    Advisor publishes the same commitment recommendation evaluated over 7, 30 and 60 days. All
    three describe the same purchase, so showing them together inflates a handful of choices
    into a wall of near-duplicates and makes the list read as more opportunity than exists.

    Keyed by the recommendation text, the term and the subscription — the things that actually
    distinguish one decision from another. The longest lookback wins: it rests on the most
    evidence and is least distorted by one unusually quiet or busy week.
    """
    best: dict[tuple, dict[str, Any]] = {}
    for it in items:
        key = (it.get("problem"), it.get("term"), it.get("subscriptionId"), it.get("sku"))
        prev = best.get(key)
        if prev is None or it["lookback_days"] > prev["lookback_days"]:
            best[key] = it
    return list(best.values())


async def rate_optimisation(subscription_ids: list[str] | None = None,
                            top: int = 100) -> dict[str, Any]:
    """Commitment-based savings Azure is currently recommending, plus the rest of Cost Advisor.

    Split rather than merged: a reservation recommendation asks for a purchase decision and a
    budget, while "delete this unattached disk" asks for ten seconds and a right-click. Showing
    them in one list makes both harder to act on.
    """
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    rows = await _arg(ADVISOR_COST_QUERY, subs, top=1000)

    commitments, usage = [], []
    for r in rows:
        item = {
            "problem": r.get("problem"),
            "solution": r.get("solution"),
            "impact": r.get("impact"),
            "type": str(r.get("impactedType") or "").split("/")[-1],
            "name": r.get("impactedName"),
            "resourceGroup": r.get("resourceGroup"),
            "subscriptionId": r.get("subscriptionId"),
            "savings": _num(r.get("savings")),
            "annual_savings": _num(r.get("annualSavings")),
            "currency": r.get("currency") or "USD",
            # A savings plan is quoted over its term, so P1Y and P3Y for the same estate are
            # alternatives rather than things to add together.
            "term": {"P1Y": "1 year", "P3Y": "3 year"}.get(r.get("term"), r.get("term") or ""),
            "lookback_days": _lookback_days(r.get("lookback")),
            "lookback": f"{r.get('lookback')} days" if r.get("lookback") else "",
            "sku": r.get("skuName"),
            "quantity": r.get("quantity"),
            "region": r.get("region"),
        }
        (commitments if _is_commitment(item["problem"]) else usage).append(item)

    # Advisor emits the same commitment at several lookback windows — 7, 30 and 60 days — and
    # listing all of them turns five real decisions into forty rows that look like forty
    # opportunities. Keep one per recommendation-and-term, preferring the longest window
    # available, which is the most evidence Advisor has and the least swayed by a quiet week.
    commitments = _best_per_option(commitments)

    commitments.sort(key=lambda x: -(x.get("savings") or 0))
    usage.sort(key=lambda x: -(x.get("savings") or 0))

    # Terms are alternatives, so the headline is the best single option rather than a sum —
    # adding a one-year and a three-year recommendation for the same workload double-counts it.
    best = max((c.get("savings") or 0 for c in commitments), default=0)
    best_term = next((c["term"] for c in commitments if (c.get("savings") or 0) == best), "")

    return {
        "commitments": commitments[:top],
        "usage": usage[:top],
        "best_commitment_saving": round(best, 2) if best else 0,
        "best_commitment_term": best_term,
        "usage_saving": round(sum(u.get("savings") or 0 for u in usage), 2),
        "currency": next(
            (r.get("currency") for r in commitments + usage if r.get("currency")), "USD"
        ),
        "scanned": {"subscriptions": len(subs), "recommendations": len(rows)},
        "source": "Azure Advisor",
        "note": (
            "From Azure Advisor, scoped to the subscriptions you can see — the same source the "
            "built-in Cost Optimization workbook uses. Terms are alternatives to choose "
            "between, not savings to add up."
        ),
        # Existing reservation utilisation needs Microsoft.Capacity/reservationOrders/read at
        # billing scope, which most people do not hold. Say so rather than showing an empty
        # panel that reads as "you have no reservations".
        "utilisation_available": False,
        "utilisation_note": (
            "How your existing reservations and savings plans are performing needs the "
            "Microsoft.Capacity/reservationOrders/read permission at billing scope, which this "
            "sign-in does not have. Opportunity is shown; current coverage is not."
        ),
    }

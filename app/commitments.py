"""Commitments: reservations, savings plans and Spot — paying a different rate for the same thing.

Three ways to pay less without using less, and one view over all of them:

  * **Reservations** — commit to a specific SKU in a region for one or three years.
  * **Savings plans** — commit to an hourly spend across compute, more flexible, less discount.
  * **Spot** — take surplus capacity at a steep discount, accept eviction.

Everything here reads the local warehouse, so it is milliseconds and answers for exactly the
subscriptions the caller is entitled to. What it can say depends on how the warehouse was
built: an **amortized** ingest spreads a reservation purchase across the term it covers and
attributes usage to the resources that consumed it, which is what makes "what did the
commitment actually save" answerable. An **actual-cost** ingest bills the purchase as one lump
on the day it happened, so the same question has no honest answer — and this module says so
rather than showing a chart of zeroes.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cloudlens.commitments")

# How Azure labels the pricing model. FOCUS and the legacy exports disagree, and a reservation
# appears as "Reservation" in one and "Committed" in another, so match on a set rather than a
# string and keep the vocabulary in one place.
COMMITTED = ("reservation", "committed", "savingsplan", "savings plan", "commitmentdiscount")
SPOT = ("spot",)
ON_DEMAND = ("ondemand", "on demand", "payg", "standard")


def _bucket(pricing_model: str | None) -> str:
    """Group a raw PricingModel value into something a person would recognise."""
    low = str(pricing_model or "").strip().lower().replace("_", "")
    if not low:
        return "unknown"
    if any(k in low for k in SPOT):
        return "spot"
    if any(k in low for k in COMMITTED):
        return "committed"
    if any(k in low for k in ON_DEMAND):
        return "on demand"
    return "other"


class Commitments:
    """Reads the warehouse. Holds no state."""

    def __init__(self) -> None:
        from .warehouse import warehouse

        self.wh = warehouse

    def coverage(self, scope: list[str] | None, days: int = 30,
                 currency: str | None = None) -> dict[str, Any]:
        """How spend splits across on-demand, committed and Spot rates."""
        with self.wh.reader(scope, currency=currency) as r:
            hi = r.rows('SELECT max("ChargePeriodStart") AS hi FROM costs')
            hi = hi[0]["hi"] if hi and hi[0]["hi"] else None
            if not hi:
                return {"empty": True, "days": days}

            start = f"date '{hi}' - INTERVAL {int(days)} DAY"
            period = f'"ChargePeriodStart" > {start}'

            split = r.rows(
                'SELECT coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost, count(DISTINCT "ResourceId") AS resources '
                f"FROM costs WHERE {period} GROUP BY 1, 2", 100)

            # Compute is where commitments actually apply, so the headline percentage is about
            # compute rather than total spend — a storage-heavy estate would otherwise look
            # permanently uncovered no matter how many VMs were reserved.
            compute = r.rows(
                'SELECT coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, sum("BilledCost") AS cost '
                "FROM costs WHERE " + period + " AND ("
                '"ServiceFamily" = \'Compute\' OR "ServiceName" ILIKE \'%virtual machine%\''
                ") GROUP BY 1, 2", 100)

            benefits = r.rows(
                'SELECT "BenefitName" AS name, coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, sum("BilledCost") AS cost, '
                'count(DISTINCT "ResourceId") AS resources '
                f'FROM costs WHERE {period} AND "BenefitName" IS NOT NULL '
                "AND \"BenefitName\" <> '' GROUP BY 1, 2, 3 ORDER BY cost DESC", 50)

            monthly = r.rows(
                'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
                'coalesce("PricingModel", \'\') AS model, '
                'sum("BilledCost") AS cost FROM costs GROUP BY 1, 2 ORDER BY 1', 400)

        def fold(rows: list[dict]) -> tuple[dict[str, float], float]:
            out: dict[str, float] = {}
            for row in rows:
                out[_bucket(row["model"])] = out.get(_bucket(row["model"]), 0.0) + float(
                    row.get("cost") or 0.0
                )
            return {k: round(v, 2) for k, v in out.items()}, round(sum(out.values()), 2)

        all_split, total = fold(split)
        compute_split, compute_total = fold(compute)

        currency = next((s.get("currency") for s in split if s.get("currency")), None)

        def pct(part: float, whole: float) -> float | None:
            return round(part / whole * 100, 1) if whole else None

        # Month on month, so a coverage change is visible rather than just today's snapshot.
        by_month: dict[str, dict[str, float]] = {}
        for row in monthly:
            m = by_month.setdefault(str(row["month"]), {})
            b = _bucket(row["model"])
            m[b] = m.get(b, 0.0) + float(row.get("cost") or 0.0)
        trend = [
            {
                "month": month,
                "on_demand": round(vals.get("on demand", 0.0), 2),
                "committed": round(vals.get("committed", 0.0), 2),
                "spot": round(vals.get("spot", 0.0), 2),
                "total": round(sum(vals.values()), 2),
                "coverage_pct": pct(vals.get("committed", 0.0), sum(vals.values())),
            }
            for month, vals in sorted(by_month.items())
        ]

        return {
            "days": days,
            "as_of": str(hi),
            "currency": currency,
            "total": total,
            "split": all_split,
            "compute_total": compute_total,
            "compute_split": compute_split,
            "coverage_pct": pct(all_split.get("committed", 0.0), total),
            "compute_coverage_pct": pct(compute_split.get("committed", 0.0), compute_total),
            "spot_pct": pct(all_split.get("spot", 0.0), total),
            "benefits": [
                {
                    "name": b["name"],
                    "model": _bucket(b["model"]),
                    "cost": round(float(b.get("cost") or 0), 2),
                    "resources": int(b.get("resources") or 0),
                    "currency": b.get("currency"),
                }
                for b in benefits
            ],
            "trend": trend[-13:],
            # Whether the warehouse can answer the commitment questions at all. Stated as data
            # rather than inferred in the UI, so the reason travels with the numbers.
            "amortized": bool(benefits) or any(
                _bucket(s["model"]) == "committed" for s in split
            ),
        }

    def spot_savings(self, scope: list[str] | None, days: int = 90,
                     currency: str | None = None) -> dict[str, Any]:
        """What Spot is saving, by comparing its rate to on-demand for the same meter.

        The comparison is per meter and per month: the same VM size bought Spot and on-demand
        in the same month gives a like-for-like unit price, and the gap times the Spot quantity
        is the saving. Where a meter has no on-demand rate to compare against, it is left out
        rather than valued at a guess.

        Converting inside the reader keeps the like-for-like property intact: both sides of every
        comparison move by the same rate, so the saving is the same number in different units.
        """
        with self.wh.reader(scope, currency=currency) as r:
            rows = r.rows(
                'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
                '"MeterName" AS meter, coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost, sum("PricingQuantity") AS qty '
                'FROM costs WHERE "PricingQuantity" > 0 '
                "GROUP BY 1, 2, 3, 4", 4000)

        spot_rows = [x for x in rows if _bucket(x["model"]) == "spot"]
        if not spot_rows:
            return {"empty": True, "reason": "No Spot usage in the warehouse."}

        # Effective unit price per meter per month for everything that is not Spot.
        base: dict[tuple, tuple[float, float]] = {}
        for x in rows:
            if _bucket(x["model"]) == "spot":
                continue
            key = (x["month"], x["meter"])
            cost, qty = base.get(key, (0.0, 0.0))
            base[key] = (cost + float(x.get("cost") or 0), qty + float(x.get("qty") or 0))

        by_month: dict[str, dict[str, float]] = {}
        priced, unpriced = 0, 0
        for x in spot_rows:
            month = str(x["month"])
            spot_cost = float(x.get("cost") or 0)
            spot_qty = float(x.get("qty") or 0)
            m = by_month.setdefault(month, {"spot_cost": 0.0, "would_have_cost": 0.0})
            m["spot_cost"] += spot_cost

            ref = base.get((x["month"], x["meter"]))
            if ref and ref[1] > 0 and spot_qty > 0:
                m["would_have_cost"] += (ref[0] / ref[1]) * spot_qty
                priced += 1
            else:
                # No on-demand rate for this meter: count the Spot cost, claim no saving.
                m["would_have_cost"] += spot_cost
                unpriced += 1

        trend = [
            {
                "month": month,
                "spot_cost": round(v["spot_cost"], 2),
                "would_have_cost": round(v["would_have_cost"], 2),
                "saved": round(v["would_have_cost"] - v["spot_cost"], 2),
                "saved_pct": (
                    round((v["would_have_cost"] - v["spot_cost"]) / v["would_have_cost"] * 100, 1)
                    if v["would_have_cost"] else None
                ),
            }
            for month, v in sorted(by_month.items())
        ]

        return {
            "trend": trend[-13:],
            "total_saved": round(sum(t["saved"] for t in trend), 2),
            "total_spot_cost": round(sum(t["spot_cost"] for t in trend), 2),
            "currency": next((x.get("currency") for x in spot_rows if x.get("currency")), None),
            "meters_priced": priced,
            "meters_unpriced": unpriced,
            "note": (
                "Saving is the gap between the Spot rate and the on-demand rate for the same "
                "meter in the same month. Meters with no on-demand usage to compare against "
                "are counted at cost and claim no saving."
            ),
        }


commitments: Commitments | None = None


def get_commitments() -> Commitments:
    global commitments
    if commitments is None:
        commitments = Commitments()
    return commitments

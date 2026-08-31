"""What a VM would save if it were only switched on when it is used.

The observation this rests on is dull and reliable: a machine billed for 168 hours a week is
often only *wanted* for about 50. Dev boxes, build agents, test rigs, demo environments — they
run at night and at weekends because nobody switched them off, not because anybody needs them.

The saving is arithmetic once you know two things: what the VM costs per running hour, and
which hours it was doing anything. The first comes from the warehouse. The second needs hourly
CPU, which is why this cannot reuse `vm_utilisation` — that samples at P1D, and a daily average
cannot tell a machine busy 09:00–18:00 from one busy at 03:00.

Three refusals, each of which stops this from being dangerous advice:

  * **A machine with a flat profile is left alone.** If CPU at 3am looks like CPU at 3pm, either
    it is genuinely serving something around the clock or it is idle throughout — and the second
    case is `find_waste`'s job, not this one. Recommending a schedule for a database replica
    because its CPU is low would be actively harmful.

  * **Production is never suggested for shutdown.** Inferred from tags where they exist. A
    saving that takes down a production system is not a saving.

  * **Savings are stated as a ceiling, not a promise.** Reserved instances, savings plans and
    per-second billing all mean the realised figure is lower. Saying "up to" is the difference
    between a credible tool and one that gets quietly ignored after the first month.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("cloudlens.shutdown")

METRICS_API = "2023-10-01"

# Hours a week a machine is billed for if nobody ever turns it off.
WEEK_HOURS = 168

# The default working window: 08:00–20:00, Monday to Friday. Twelve hours rather than eight
# because a schedule that ends the moment someone might still be working is one that gets
# switched off within a fortnight.
BUSINESS_START = 8
BUSINESS_END = 20
BUSINESS_HOURS = (BUSINESS_END - BUSINESS_START) * 5   # 60 of 168

# CPU below this counts as "not doing anything" for the purposes of the profile.
IDLE_CPU = 3.0

# How much busier the working window must be than the rest before a schedule is suggested. 3x
# rather than something marginal: a machine that is only slightly quieter at night is doing
# *something* at night, and the cost of being wrong here is an outage.
BUSY_RATIO = 3.0

# Below this there is nothing worth automating.
MIN_MONTHLY_SAVING = 5.0

# How much of a VM's monthly line is actually compute, and therefore avoidable by deallocating.
# Disks and reserved IPs keep billing while the machine is off, so treating the whole line as
# avoidable overstates the saving — which is discovered the following month, exactly when trust
# in the number is lost. 0.8 is conservative for a small VM with one managed disk.
COMPUTE_SHARE = 0.8

# Tags that mean "do not touch". Checked case-insensitively on both key and value.
PRODUCTION_HINTS = ("prod", "production", "live")


class ShutdownError(RuntimeError):
    """Something the caller can act on."""


def is_production(tags: dict[str, Any] | None) -> bool:
    """Whether anything about this resource says production.

    Deliberately broad. A false positive costs one missed saving; a false negative proposes
    switching off something that serves customers.
    """
    for key, value in (tags or {}).items():
        text = f"{key} {value}".lower()
        if any(hint in text for hint in PRODUCTION_HINTS):
            return True
    return False


def profile(hourly: list[dict[str, Any]]) -> dict[str, Any]:
    """Split hourly CPU samples into working hours and the rest.

    Returns the two averages, how many hours were busy outside the window, and whether the
    shape is pronounced enough to act on.
    """
    inside: list[float] = []
    outside: list[float] = []
    busy_outside = 0

    for point in hourly:
        when = point.get("when")
        cpu = point.get("cpu")
        if when is None or cpu is None:
            continue
        working = when.weekday() < 5 and BUSINESS_START <= when.hour < BUSINESS_END
        if working:
            inside.append(cpu)
        else:
            outside.append(cpu)
            if cpu >= IDLE_CPU:
                busy_outside += 1

    if not inside or not outside:
        return {"enough_data": False}

    avg_in = sum(inside) / len(inside)
    avg_out = sum(outside) / len(outside)

    # The ratio, guarded against a zero denominator: a machine at literally 0% outside hours is
    # the strongest possible signal, not a division error.
    ratio = (avg_in / avg_out) if avg_out > 0.01 else (BUSY_RATIO + 1 if avg_in > IDLE_CPU else 0)

    return {
        "enough_data": True,
        "avg_business": round(avg_in, 1),
        "avg_offhours": round(avg_out, 1),
        "ratio": round(ratio, 1),
        "busy_offhours_hours": busy_outside,
        "samples": len(inside) + len(outside),
        # Both conditions: busy enough during the day to be worth having, and quiet enough at
        # night that turning it off is not an outage.
        "schedulable": ratio >= BUSY_RATIO and avg_in >= IDLE_CPU and busy_outside <= len(outside) * 0.05,
    }


def saving(monthly_cost: float, hours_kept: int = BUSINESS_HOURS) -> dict[str, Any]:
    """What a schedule would save, stated as a ceiling.

    Compute only. A VM's bill includes disks and IP addresses that keep charging while it is
    deallocated, so treating the whole line as avoidable overstates the saving — and an
    overstated saving is discovered the following month, which is exactly when trust is lost.
    Disks are typically a fifth of a small VM's cost, so the compute share is discounted.
    """
    keep = max(0, min(hours_kept, WEEK_HOURS)) / WEEK_HOURS
    compute = monthly_cost * COMPUTE_SHARE
    return {
        "current": round(monthly_cost, 2),
        "scheduled": round(compute * keep + (monthly_cost - compute), 2),
        "saving": round(compute * (1 - keep), 2),
        "hours_kept": hours_kept,
        "hours_saved": WEEK_HOURS - hours_kept,
    }


async def _hourly_cpu(vms: list[dict[str, Any]], days: int) -> dict[str, list[dict[str, Any]]]:
    """Hourly CPU per VM. PT1H, not P1D — the whole question is *when*, not *how much*.

    Fourteen days is the default because the batch endpoint returns one point per hour per
    machine: a month across fifty VMs is 36,000 points, which is slow to fetch and no more
    informative than a fortnight for establishing a daily rhythm.
    """
    from .cost import azure

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    groups: dict[tuple[str, str], list[str]] = {}
    for v in vms:
        groups.setdefault((v["location"], v["subscriptionId"]), []).append(v["id"])

    async def batch(region: str, sub: str, ids: list[str]) -> dict[str, list]:
        out: dict[str, list] = {}
        for chunk in (ids[i:i + 50] for i in range(0, len(ids), 50)):
            try:
                payload = await azure.request(
                    "POST",
                    f"https://{region}.metrics.monitor.azure.com/subscriptions/{sub}"
                    "/metrics:getBatch",
                    params={
                        "api-version": METRICS_API,
                        "metricnamespace": "Microsoft.Compute/virtualMachines",
                        "metricnames": "Percentage CPU",
                        "aggregation": "average",
                        "interval": "PT1H",
                        "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "endtime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    json_body={"resourceids": chunk},
                    cache=False,
                )
            except Exception as exc:  # noqa: BLE001 - one region must not fail the whole scan
                log.warning("hourly metrics failed for %s/%s: %s", region, sub, str(exc)[:160])
                continue

            for entry in payload.get("values", []):
                for metric in entry.get("value", []):
                    rid = str(metric.get("id", "")).split("/providers/Microsoft.Insights")[0]
                    series = (metric.get("timeseries") or [{}])[0].get("data", [])
                    points = []
                    for p in series:
                        if p.get("average") is None:
                            continue
                        try:
                            when = datetime.fromisoformat(
                                str(p["timeStamp"]).replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        points.append({"when": when, "cpu": float(p["average"])})
                    if points:
                        out[rid.lower()] = points
        return out

    results = await asyncio.gather(
        *(batch(r, s, ids) for (r, s), ids in groups.items()), return_exceptions=True)
    merged: dict[str, list] = {}
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
    return merged


async def find_schedulable(subscription_ids: list[str] | None = None,
                           days: int = 14) -> dict[str, Any]:
    """VMs running around the clock that are only used during the working day.

    The output is deliberately a shortlist rather than an inventory. Every row is something
    somebody could act on this afternoon, and anything that needs a judgement call about
    whether it is safe has already been excluded.
    """
    from .waste import _arg, _costs_for
    from .cost import list_subscriptions

    days = max(7, min(int(days), 30))
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]

    vms = await _arg(
        """
        Resources
        | where type =~ 'microsoft.compute/virtualmachines'
        | extend state = tostring(properties.extended.instanceView.powerState.code)
        | where state =~ 'PowerState/running'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  size = tostring(properties.hardwareProfile.vmSize)
        """,
        subs,
    )
    if not vms:
        return {"candidates": [], "count": 0, "total_saving": 0.0,
                "note": "No running virtual machines in this scope."}

    hourly = await _hourly_cpu(vms, days)
    costs = _costs_for([v["id"] for v in vms], 30)

    candidates: list[dict[str, Any]] = []
    skipped_prod = 0
    skipped_flat = 0
    no_data = 0

    for v in vms:
        points = hourly.get(v["id"].lower(), [])
        shape = profile(points)
        if not shape.get("enough_data"):
            no_data += 1
            continue

        prod = is_production(v.get("tags"))
        if prod:
            skipped_prod += 1
            continue
        if not shape["schedulable"]:
            skipped_flat += 1
            continue

        monthly = float((costs.get(v["id"].lower()) or {}).get("cost") or 0.0)
        numbers = saving(monthly)
        if numbers["saving"] < MIN_MONTHLY_SAVING:
            continue

        candidates.append({
            "name": v["name"],
            "id": v["id"],
            "resource_group": v.get("resourceGroup"),
            "subscription_id": v.get("subscriptionId"),
            "size": v.get("size"),
            "location": v.get("location"),
            **shape,
            **numbers,
            "window": f"{BUSINESS_START:02d}:00–{BUSINESS_END:02d}:00 Mon–Fri",
        })

    candidates.sort(key=lambda c: c["saving"], reverse=True)
    currency = next((c.get("currency") for c in costs.values() if c.get("currency")), "USD")

    return {
        "candidates": candidates,
        "count": len(candidates),
        "total_saving": round(sum(c["saving"] for c in candidates), 2),
        "total_current": round(sum(c["current"] for c in candidates), 2),
        "currency": currency,
        "examined": len(vms),
        "skipped_production": skipped_prod,
        "skipped_steady": skipped_flat,
        "no_metrics": no_data,
        "window": f"{BUSINESS_START:02d}:00–{BUSINESS_END:02d}:00 Mon–Fri",
        "hours_kept": BUSINESS_HOURS,
        "days_analysed": days,
        "note": None if candidates else (
            f"Examined {len(vms)} running VM(s). None showed a clear daily rhythm: "
            f"{skipped_flat} are busy around the clock or idle throughout, {skipped_prod} are "
            f"tagged production, and {no_data} had no hourly metrics."
        ),
    }

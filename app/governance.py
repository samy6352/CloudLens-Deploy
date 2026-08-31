"""Estate hygiene: configuration that is wrong rather than spend that is wasted.

The waste module answers "what am I paying for and not using". This one answers a different
question — "what is configured in a way I would not choose today" — which covers two things
people ask for constantly:

  * **Tagging.** An untagged resource cannot be charged back, filtered in a report, or found
    by its owner. Tag coverage is the single number most governance conversations start with.
  * **Accelerated networking.** Off by default on older deployments, and a free performance
    win when the size supports it — lower latency, lower jitter, less CPU spent on packets.

Both are Resource Graph queries, so this is a live tab: seconds, not milliseconds. It is kept
out of the Networking *cost* tab deliberately — that one is a warehouse query that returns
instantly, and bolting a live Azure scan onto it would make every visit slow.

Everything here is read-only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cost import ARM, azure, list_subscriptions
from .waste import _arg, _costs_for

log = logging.getLogger("cloudlens.governance")

COMPUTE_API = "2024-07-01"

# Resource types that exist only as children of something else, or that Azure creates and
# manages itself. Counting these as "untagged" produces a number nobody can act on: you cannot
# tag a VM extension, and a Logic App connection inherits its parent's ownership.
NOT_TAGGABLE = (
    "microsoft.compute/virtualmachines/extensions",
    "microsoft.classiccompute/domainnames",
    "microsoft.insights/autoscalesettings",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.security/automations",
)

UNTAGGED_QUERY = """
    Resources
    | where isnull(tags) or array_length(bag_keys(tags)) == 0
    | project id, name, type, resourceGroup, subscriptionId, location
"""

TAG_COVERAGE_QUERY = """
    Resources
    | extend n = array_length(bag_keys(tags))
    | summarize total = count(), tagged = countif(isnotnull(tags) and n > 0) by type
    | order by total desc
"""

# A NIC carries the flag, but the *VM size* decides whether it can be turned on at all, so the
# size is projected here and checked against the SKU catalogue below.
#
# The join key is lowercased on both sides. Azure returns the same resource id with different
# casing depending on which resource you asked — a NIC reports its VM as `resourceGroups/tub`
# while the VM itself reports `resourceGroups/TUB` — and Kusto's join is case-sensitive, so
# joining on the raw ids silently matches nothing. The cost join in waste.py has the same
# defence for the same reason.
ACCEL_QUERY = """
    Resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | extend vmId = tolower(tostring(properties.virtualMachine.id))
    | where isnotempty(vmId)
    | extend accel = tobool(properties.enableAcceleratedNetworking)
    | project nicId = id, nicName = name, resourceGroup, subscriptionId, location, accel, vmId
    | join kind=inner (
        Resources
        | where type =~ 'microsoft.compute/virtualmachines'
        | project vmId = tolower(tostring(id)), vmName = name,
                  vmSize = tostring(properties.hardwareProfile.vmSize),
                  powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
      ) on vmId
    | project nicId, nicName, resourceGroup, subscriptionId, location, accel,
              vmId, vmName, vmSize, powerState
"""


async def _accel_capable_sizes(subscription_id: str, region: str) -> set[str]:
    """VM sizes that support accelerated networking, from Azure's own SKU catalogue.

    Without this the finding is noise: a B-series or single-vCPU machine *cannot* enable it, so
    reporting one as "missing accelerated networking" sends someone to a blade with no such
    checkbox. Asking Azure which sizes are capable makes the answer true.

    Filtered to one region, which took this from 38 seconds to under two. Unfiltered, the SKU
    endpoint returns every size in every region — tens of thousands of entries, paginated — and
    whether a size supports accelerated networking does not vary by region anyway. A size that
    is not offered in the sampled region simply will not appear, which fails towards reporting
    it rather than hiding it.

    Returned lowercase, because Resource Graph and the SKU API disagree on casing.
    """
    data = await azure.request(
        "GET",
        f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus",
        params={
            "api-version": COMPUTE_API,
            "$filter": f"location eq '{region}'",
        },
    )
    capable: set[str] = set()
    for sku in data.get("value", []):
        if sku.get("resourceType") != "virtualMachines":
            continue
        for cap in sku.get("capabilities", []):
            if cap.get("name") == "AcceleratedNetworkingEnabled" and str(
                cap.get("value", "")
            ).lower() == "true":
                capable.add(str(sku.get("name", "")).lower())
                break
    return capable


async def tagging(subscription_ids: list[str] | None = None,
                  days: int = 30, top: int = 200) -> dict[str, Any]:
    """Tag coverage across the estate, and what the untagged resources are costing."""
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    coverage, untagged = await asyncio.gather(
        _arg(TAG_COVERAGE_QUERY, subs, top=500),
        _arg(UNTAGGED_QUERY, subs, top=2000),
        return_exceptions=True,
    )
    if isinstance(coverage, Exception):
        raise coverage
    if isinstance(untagged, Exception):
        raise untagged

    # Drop the types nobody can tag, from both the list and the coverage maths, so the
    # percentage means "of things I could have tagged" rather than being permanently short.
    untagged = [r for r in untagged if str(r.get("type", "")).lower() not in NOT_TAGGABLE]
    coverage = [c for c in coverage if str(c.get("type", "")).lower() not in NOT_TAGGABLE]

    costs = _costs_for([r["id"] for r in untagged], days)
    for r in untagged:
        hit = costs.get(str(r["id"]).lower(), {})
        r["cost"] = hit.get("cost")
        r["currency"] = hit.get("currency")

    total = sum(int(c.get("total") or 0) for c in coverage)
    tagged = sum(int(c.get("tagged") or 0) for c in coverage)

    by_type = [
        {
            "type": c["type"].split("/")[-1],
            "full_type": c["type"],
            "total": int(c.get("total") or 0),
            "tagged": int(c.get("tagged") or 0),
            "untagged": int(c.get("total") or 0) - int(c.get("tagged") or 0),
            "coverage_pct": (
                round(int(c.get("tagged") or 0) / int(c["total"]) * 100, 1)
                if c.get("total") else None
            ),
        }
        for c in coverage
    ]
    by_type = [t for t in by_type if t["untagged"]]
    by_type.sort(key=lambda t: -t["untagged"])

    # Which tag keys are actually in use, so someone can see the convention they already have
    # rather than inventing a new one.
    keys = await _arg(
        "Resources | where isnotnull(tags) | mv-expand tags "
        "| extend k = tostring(bag_keys(tags)[0]) | summarize n = count() by k "
        "| order by n desc | limit 15",
        subs, top=15,
    )

    priced = [r for r in untagged if r.get("cost")]
    priced.sort(key=lambda r: -(r.get("cost") or 0))

    return {
        "days": days,
        "scanned": {"resources": total, "tagged": tagged},
        "coverage_pct": round(tagged / total * 100, 1) if total else None,
        "untagged_total": total - tagged,
        "untagged_cost": round(sum(r.get("cost") or 0 for r in priced), 2),
        "currency": next((r.get("currency") for r in priced if r.get("currency")), None),
        "by_type": by_type[:20],
        "tag_keys": [{"key": k.get("k"), "count": int(k.get("n") or 0)} for k in keys if k.get("k")],
        "resources": priced[:top],
        "note": (
            "Coverage excludes resource types that cannot carry their own tags, such as VM "
            "extensions — counting those would make the figure permanently unreachable."
        ),
    }


async def accelerated_networking(subscription_ids: list[str] | None = None,
                                 top: int = 200) -> dict[str, Any]:
    """VMs whose NIC could have accelerated networking on, and does not."""
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    nics = await _arg(ACCEL_QUERY, subs, top=2000)
    if not nics:
        return {"scanned": {"nics": 0}, "eligible": [], "enabled": 0, "not_supported": 0}

    enabled = [n for n in nics if n.get("accel")]
    off = [n for n in nics if not n.get("accel")]

    # The catalogue is only needed to filter candidates. With none, the call is pure latency —
    # and on an estate where everything is already enabled, that was the whole cost of the tab.
    capable: set[str] = set()
    if off:
        region = next(
            (str(n["location"]).lower() for n in off if n.get("location")), "eastus"
        )
        try:
            capable = await _accel_capable_sizes(subs[0], region)
        except Exception as exc:  # noqa: BLE001
            # Without the catalogue the finding would be a guess, so say so rather than listing
            # machines that may have no such setting.
            log.warning("accelerated networking: SKU catalogue unavailable: %s", str(exc)[:200])

    eligible, unsupported = [], []
    for n in off:
        size = str(n.get("vmSize", "")).lower()
        (eligible if (not capable or size in capable) else unsupported).append(n)

    for n in eligible:
        n["vm"] = n.get("vmName")
        n["nic"] = n.get("nicName")

    return {
        "scanned": {"nics": len(nics), "vms": len({n.get("vmId") for n in nics})},
        "enabled": len(enabled),
        "not_supported": len(unsupported),
        "catalogue": bool(capable),
        "eligible": eligible[:top],
        "note": (
            "Only sizes Azure lists as accelerated-networking capable are counted. Turning it "
            "on requires the VM to be deallocated first."
            if capable else
            "The VM size catalogue could not be read, so sizes that cannot support accelerated "
            "networking may appear here. Verify the size before changing anything."
        ),
    }

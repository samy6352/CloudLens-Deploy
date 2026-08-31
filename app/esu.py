"""Extended Security Updates: what is out of support, and what that costs.

ESU is the one cost that does not show up until it is too late to plan for. A server running
Windows Server 2012 R2 bills exactly the same as one running 2022 — right up until support ends,
at which point you are either paying for ESU or running unpatched. Neither shows in a cost
report as "the 2012 problem".

Two rules decide the money, and they are the ones people get wrong:

- **In Azure, ESU is included.** An Azure VM on Windows Server 2012/2012 R2 (or 2008/R2) gets
  Extended Security Updates at no additional charge. Migrating a server *into* Azure is itself
  the ESU purchase.
- **Outside Azure, ESU is billed per core**, delivered through Azure Arc, at rates that depend on
  edition and year. That is where a bill appears.

So this reports three separate things and does not blur them: what is out of support, what is
already covered, and what an uncovered machine would cost. Prices come from the live Azure retail
catalogue rather than a constant in this file — ESU rates change by year of the programme, and a
hardcoded number would quietly become a lie.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import httpx

log = logging.getLogger("cloudlens.esu")

PRICES_URL = "https://prices.azure.com/api/retail/prices"

# Microsoft lifecycle end-of-extended-support dates. Reviewed Aug 2026; these move only when
# Microsoft announces a change, and a wrong date here shows up as a wrong status, so they are
# kept in one visible table rather than scattered through the logic.
LIFECYCLE: list[tuple[str, str, date]] = [
    ("Windows Server 2008 R2", r"2008.?r2", date(2020, 1, 14)),
    ("Windows Server 2008", r"2008", date(2020, 1, 14)),
    ("Windows Server 2012 R2", r"2012.?r2", date(2023, 10, 10)),
    ("Windows Server 2012", r"2012", date(2023, 10, 10)),
    ("Windows Server 2016", r"2016", date(2027, 1, 12)),
    ("Windows Server 2019", r"2019", date(2029, 1, 9)),
    ("Windows Server 2022", r"2022", date(2031, 10, 14)),
    ("Windows Server 2025", r"2025", date(2034, 10, 10)),
]

SQL_LIFECYCLE: list[tuple[str, str, date]] = [
    # Anchored to the "SQL" prefix on purpose: an image offer reads "SQL2019-WS2016", so a bare
    # year would match the Windows version and report the wrong product as out of support.
    ("SQL Server 2012", r"sql.{0,2}2012", date(2022, 7, 12)),
    ("SQL Server 2014", r"sql.{0,2}2014", date(2024, 7, 9)),
    ("SQL Server 2016", r"sql.{0,2}2016", date(2026, 7, 14)),
    ("SQL Server 2017", r"sql.{0,2}2017", date(2027, 10, 12)),
    ("SQL Server 2019", r"sql.{0,2}2019", date(2030, 1, 8)),
    ("SQL Server 2022", r"sql.{0,2}2022", date(2033, 1, 11)),
]

# How close to the end counts as "plan for this now".
SOON_DAYS = 400


def classify(text: str, table: list[tuple[str, str, date]] | None = None,
             today: date | None = None) -> dict[str, Any] | None:
    """Map an OS or SQL image string to a product and its support status.

    Returns None when nothing recognisable is found, which is the common case (Linux, Windows
    client, appliance images) and must not be reported as a problem.
    """
    if not text:
        return None
    haystack = text.lower()
    today = today or date.today()

    for name, pattern, ends in (table or LIFECYCLE):
        if re.search(pattern, haystack):
            remaining = (ends - today).days
            status = ("out of support" if remaining < 0
                      else "ending soon" if remaining <= SOON_DAYS
                      else "supported")
            return {"product": name, "support_ends": ends.isoformat(),
                    "days_remaining": remaining, "status": status}
    return None


def _is_windows_server(publisher: str, offer: str) -> bool:
    text = f"{publisher} {offer}".lower()
    return "windowsserver" in text.replace(" ", "") or "windows-server" in text


async def esu_prices() -> dict[str, float]:
    """Per-core-hour ESU rates from the retail catalogue, keyed `<product>|<edition>`.

    Published in packs (1C, 8C, 16C); we normalise to a single core so any core count can be
    priced. Empty on failure — an unavailable price list must degrade to "no estimate", never
    to a made-up one.
    """
    params = {"currencyCode": "'USD'", "$filter": "contains(productName,'Extended Security')"}
    rates: dict[str, float] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(PRICES_URL, params=params)
            r.raise_for_status()
            items = r.json().get("Items", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read ESU prices: %s", str(exc)[:150])
        return {}

    for item in items:
        sku = str(item.get("skuName", ""))
        if item.get("armRegionName") not in ("Global", "", None):
            continue
        if "Back Billing" in str(item.get("meterName", "")):
            continue  # a catch-up charge, not the running rate
        m = re.match(r"WS(\d{4})\s+(DC|Std)\s+(\d+)C", sku)
        if not m:
            continue
        year, edition, cores = m.group(1), m.group(2), int(m.group(3))
        price = float(item.get("retailPrice") or 0.0)
        if not price or not cores:
            continue
        key = f"Windows Server {year}|{'Datacenter' if edition == 'DC' else 'Standard'}"
        per_core = price / cores
        # Packs differ slightly per core; keep the cheapest, so an estimate never overstates.
        rates[key] = min(rates.get(key, per_core), per_core)
    return rates


def _rate_for(product: str, rates: dict[str, float], edition: str = "Standard") -> float | None:
    """The per-core-hour rate for a product, allowing for how the meters are named.

    The catalogue prices `WS2012`, and that meter covers Windows Server 2012 *and* 2012 R2 —
    so an R2 machine, which is the common ESU case, must fall back to the base year rather than
    silently come back unpriced.
    """
    for key in (f"{product}|{edition}", f"{product.replace(' R2', '')}|{edition}"):
        if key in rates:
            return rates[key]
    return None


async def esu_report(subscription_ids: list[str] | None = None,
                     days: int = 30) -> dict[str, Any]:
    """Machines whose OS or SQL version is out of support or close to it, and the ESU position."""
    from .cost import list_subscriptions
    from .waste import _arg
    from .warehouse import warehouse

    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"machines": [], "count": 0, "note": "No subscriptions in scope."}

    vm_q = (
        "resources | where type =~ 'microsoft.compute/virtualmachines' "
        "| extend img = properties.storageProfile.imageReference "
        "| project name, id, resourceGroup, location, subscriptionId, "
        "publisher=tostring(img.publisher), offer=tostring(img.offer), sku=tostring(img.sku), "
        "vmSize=tostring(properties.hardwareProfile.vmSize)"
    )
    arc_q = (
        "resources | where type =~ 'microsoft.hybridcompute/machines' "
        "| project name, id, resourceGroup, location, subscriptionId, "
        "osName=tostring(properties.osName), osSku=tostring(properties.osSku), "
        "status=tostring(properties.status), "
        "cores=tostring(properties.detectedProperties.logicalCoreCount), "
        "esuState=tostring(properties.licenseProfile.esuProfile.licenseAssignmentState), "
        "esuEligibility=tostring(properties.licenseProfile.esuProfile.esuEligibility)"
    )
    sql_q = (
        "resources | where type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines' "
        "| project name, id, resourceGroup, location, subscriptionId, "
        "image=tostring(properties.sqlImageOffer), edition=tostring(properties.sqlImageSku)"
    )

    vms, arcs, sqls, rates = await asyncio.gather(
        _arg(vm_q, subs, top=500),
        _arg(arc_q, subs, top=500),
        _arg(sql_q, subs, top=500),
        esu_prices(),
        return_exceptions=True,
    )
    for name, value in (("vms", vms), ("arc", arcs), ("sql", sqls)):
        if isinstance(value, Exception):
            log.warning("ESU inventory query %s failed: %s", name, str(value)[:150])
    vms = vms if isinstance(vms, list) else []
    arcs = arcs if isinstance(arcs, list) else []
    sqls = sqls if isinstance(sqls, list) else []
    rates = rates if isinstance(rates, dict) else {}

    machines: list[dict[str, Any]] = []

    for vm in vms:
        if not _is_windows_server(vm.get("publisher", ""), vm.get("offer", "")):
            continue
        found = classify(f"{vm.get('offer', '')} {vm.get('sku', '')}")
        if not found or found["status"] == "supported":
            continue
        machines.append({
            **found,
            "name": vm.get("name"), "id": vm.get("id"),
            "resource_group": vm.get("resourceGroup"), "location": vm.get("location"),
            "kind": "Azure VM", "detail": vm.get("sku"),
            # The rule that saves people money: running it in Azure is the ESU entitlement.
            "covered": True,
            "coverage": "Included — ESU is free for Azure VMs",
            "monthly_esu_cost": 0.0,
        })

    for arc in arcs:
        found = classify(f"{arc.get('osSku', '')} {arc.get('osName', '')}")
        if not found or found["status"] == "supported":
            continue
        state = (arc.get("esuState") or "NotAssigned")
        covered = state.lower() == "assigned"
        cores = int(arc.get("cores") or 0)
        # Arc ESU is licensed per core with an 8-core floor per machine.
        billable = max(cores, 8) if cores else 8
        rate = _rate_for(found["product"], rates)
        estimate = round(rate * billable * 730, 2) if rate else None
        machines.append({
            **found,
            "name": arc.get("name"), "id": arc.get("id"),
            "resource_group": arc.get("resourceGroup"), "location": arc.get("location"),
            "kind": "Arc-connected server", "detail": arc.get("osSku") or arc.get("osName"),
            "cores": cores or None,
            "covered": covered,
            "coverage": ("ESU licence assigned" if covered else
                         f"No ESU licence — {state}"),
            "monthly_esu_cost": 0.0 if covered else estimate,
            "connection": arc.get("status"),
        })

    for sql in sqls:
        found = classify(f"{sql.get('image', '')} {sql.get('edition', '')}", SQL_LIFECYCLE)
        if not found or found["status"] == "supported":
            continue
        machines.append({
            **found,
            "name": sql.get("name"), "id": sql.get("id"),
            "resource_group": sql.get("resourceGroup"), "location": sql.get("location"),
            "kind": "SQL Server on Azure VM", "detail": sql.get("edition"),
            "covered": True,
            "coverage": "Included — SQL ESU is free on Azure VMs",
            "monthly_esu_cost": 0.0,
        })

    # What ESU is actually being billed today, as opposed to what it might cost.
    billed, currency = 0.0, None
    try:
        rows = warehouse.query(
            'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS billed FROM costs '
            "WHERE lower(\"MeterName\") LIKE '%esu%' "
            "OR lower(\"ProductName\") LIKE '%extended security%' GROUP BY 1",
            scope=subscription_ids)["rows"]
        for row in rows:
            billed += float(row["billed"] or 0.0)
            currency = currency or row["currency"]
    except Exception as exc:  # noqa: BLE001 - the inventory is still worth showing
        log.warning("ESU spend lookup failed: %s", str(exc)[:150])

    machines.sort(key=lambda m: (m["covered"], m["days_remaining"]))
    exposed = [m for m in machines if not m["covered"]]
    estimate_total = sum(m["monthly_esu_cost"] or 0.0 for m in exposed)

    return {
        "machines": machines,
        "count": len(machines),
        "out_of_support": len([m for m in machines if m["status"] == "out of support"]),
        "ending_soon": len([m for m in machines if m["status"] == "ending soon"]),
        "exposed": len(exposed),
        "estimated_monthly_cost": round(estimate_total, 2) if estimate_total else 0.0,
        "billed_esu": round(billed, 2),
        "currency": currency or "USD",
        "priced": bool(rates),
        "scanned": {"vms": len(vms), "arc": len(arcs), "sql": len(sqls)},
        "subscriptions": len(subs),
        "note": (
            "ESU is included at no extra charge for Azure VMs; it is billed per core, through "
            "Azure Arc, for machines outside Azure. Estimates use the live Azure retail rate for "
            "Windows Server Standard with the 8-core minimum, at 730 hours a month — check your "
            "agreement for negotiated pricing."
        ),
    }

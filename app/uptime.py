"""What an hour of this estate costs, and how many hours it was actually up.

Azure does not bill by the hour — it bills by the *day*. `ChargePeriodStart` is a DATE, so
there is no honest way to draw "spend at 3am". Anyone who does is inventing a shape.

What Azure *does* give you is better than a clock face. Time-metered rows carry the number of
hours consumed in `PricingQuantity` against a `UnitOfMeasure` of "1 Hour" or "1/Hour", so for
every hour-billed resource the export states, per day, exactly how long it ran. That yields
two things a daily cost column cannot:

  * **An effective hourly rate** — cost divided by hours actually consumed. A VM that cost £40
    over a month tells you nothing; £0.31 an hour tells you what leaving it on costs, and is
    the only figure a shutdown schedule can be argued from.

  * **Uptime** — hours billed against hours available. A resource that was up 168 hours in a
    seven-day span was never off; one that was up 60 was already on a schedule. This is
    measured, not modelled: a stopped VM emits no usage, so the hours simply are not there.

Three things make the arithmetic less obvious than it looks, and each was a wrong answer first:

  * **A "1 Hour" row is not always one instance.** A two-node scale set bills 48 hours a day,
    a load balancer with two rule sets the same. Dividing by 24 would report 200% uptime. The
    instance count is recovered from the busiest day observed — the peak is what "fully on"
    looks like for that resource — and capacity is measured against that.

  * **Composite units are not time.** "1 GiB Hour" and "1 GiB Second" are GiB-hours: a quantity
    of storage multiplied by a duration. Counting them as hours would have this estate running
    ten million hours a day. Only pure time units are admitted.

  * **A resource that did not exist yet was not "switched off".** Uptime is measured across the
    span between a resource's first and last billed day, not across the whole window, so
    something created on Tuesday is not reported as having been down since Monday.

The savings model is left to the client on purpose. "What would this cost on a 12x5 schedule"
is one multiplication over data already sent, and putting it here would mean a round trip every
time someone changed the schedule they were considering.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cloudlens.uptime")

# Enough for a large estate; beyond this the response says it is partial rather than quietly
# dropping the tail of a table someone is reading as complete.
MAX_ROWS = 4000

# Per-meter-per-day cells, sent so the tab can redraw its charts under a filter without asking
# the server again. A filter that changed the table but left the charts showing the whole estate
# would be answering a question nobody asked, so the detail has to be here — but a large estate
# over 90 days can reach six figures, and past this the charts fall back to estate-wide totals
# and the tab says so rather than drawing a series from a silently truncated half of the data.
MAX_DAILY_CELLS = 150_000

# Pure time units only: "1 Hour", "1/Hour", "10 Hours", "100 Hours". Deliberately anchored at
# both ends so "1 GiB Hour" and "1 GiB Second" — which are quantity x duration, not duration —
# cannot match. See the module docstring.
HOUR_UNIT = r'^[0-9]+(\.[0-9]+)?\s*/?\s*hours?$'

# The leading multiplier: a "10 Hours" row of quantity 3 is 30 hours, not 3.
HOUR_MULTIPLIER = r'^[0-9]+(\.[0-9]+)?'

# A day is fully covered at 24 hours per instance. Billing rounds, and a resource restarted
# mid-day loses a few seconds either side, so anything from here up is "always on" rather than
# demanding an exact 24.000.
ALWAYS_ON = 0.97

# Services where a schedule is a real option, matched on a substring of the service name.
# Deliberately conservative: a managed search index or a Defender node cannot be stopped
# overnight, and offering a saving that cannot be banked is worse than offering none. Anything
# unmatched still appears with its hours and its rate — it is just not counted as schedulable.
SCHEDULABLE = (
    "virtual machine", "azure app service", "app service", "azure databricks", "synapse",
    "azure database for", "sql database", "sql managed instance", "hdinsight",
    "azure machine learning", "azure vmware", "cloud services", "azure lab",
    "azure dev", "virtual desktop", "bastion", "azure spring", "container instances",
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def hourly_cost(scope: list[str] | None = None, days: int = 30,
                currency: str | None = None) -> dict[str, Any]:
    """Billed hours, effective hourly rates and measured uptime for the window."""
    from .warehouse import warehouse

    with warehouse.reader(scope=scope, currency=currency) as r:
        # Anchored to the newest data rather than to today, for the same reason every other
        # warehouse view is: cost lands a day or two late, so a window ending at CURRENT_DATE
        # drops the most recent complete day and, on a stale warehouse, returns nothing at all
        # — which reads as "nothing ran" instead of "this data is old".
        edge = r.rows('SELECT max("ChargePeriodStart") AS hi FROM costs')
        hi = edge[0]["hi"] if edge and edge[0]["hi"] else None
        if hi is None:
            return _empty(days)

        window = f"\"ChargePeriodStart\" >= date '{hi}' - INTERVAL {int(days)} DAY"

        # Every row in the window, so the tab can say what share of spend is hour-billed at
        # all. Without it a small hourly total is indistinguishable from a small estate.
        totals = r.rows(
            f"""
            SELECT COALESCE("BillingCurrency", 'unknown') AS currency,
                   SUM(COALESCE("BilledCost", 0))         AS cost
            FROM costs WHERE {window} GROUP BY 1
            """,
            limit=64,
        )

        hourly = f"""
            SELECT
              COALESCE(NULLIF("ResourceId", ''), "ResourceName", '(unnamed)') AS id,
              COALESCE(NULLIF("ResourceName", ''), '(unnamed)')               AS name,
              COALESCE("ServiceName", '')                                     AS service,
              COALESCE("SubAccountName", '')                                  AS subscription,
              COALESCE("ResourceGroup", '')                                   AS resource_group,
              COALESCE("RegionName", '')                                      AS region,
              COALESCE("MeterName", '')                                       AS meter,
              COALESCE("BillingCurrency", 'unknown')                          AS currency,
              "ChargePeriodStart"                                             AS day,
              COALESCE("PricingQuantity", 0) * COALESCE(
                TRY_CAST(regexp_extract(trim("UnitOfMeasure"), '{HOUR_MULTIPLIER}') AS DOUBLE),
                1) AS hours,
              COALESCE("BilledCost", 0)                                       AS cost
            FROM costs
            WHERE {window}
              AND regexp_matches(lower(trim(COALESCE("UnitOfMeasure", ''))), '{HOUR_UNIT}')
        """

        # One row per resource, meter and day is the grain everything else is built from: the
        # peak day gives the instance count, and the day list gives the weekday profile.
        rows = r.rows(
            f"""
            WITH src AS ({hourly}),
                 daily AS (
                   SELECT id, meter, currency, day,
                          SUM(hours) AS hours, SUM(cost) AS cost,
                          MAX(name) AS name, MAX(service) AS service,
                          MAX(subscription) AS subscription,
                          MAX(resource_group) AS resource_group, MAX(region) AS region
                   FROM src GROUP BY id, meter, currency, day
                 )
            SELECT id, meter, currency,
                   MAX(name)            AS name,
                   MAX(service)         AS service,
                   MAX(subscription)    AS subscription,
                   MAX(resource_group)  AS resource_group,
                   MAX(region)          AS region,
                   SUM(hours)           AS hours,
                   SUM(cost)            AS cost,
                   MAX(hours)           AS peak_day_hours,
                   COUNT(*)             AS days_billed,
                   MIN(day)             AS first_day,
                   MAX(day)             AS last_day
            FROM daily
            GROUP BY id, meter, currency
            HAVING SUM(hours) > 0
            ORDER BY cost DESC
            """,
            limit=MAX_ROWS + 1,
        )

        # The estate day by day. Two series in one pass: what the hour-billed part of the
        # estate cost, and how many hours it ran — their ratio is the effective rate, and it
        # moving is the signal that something was resized rather than merely left on.
        daily = r.rows(
            f"""
            WITH src AS ({hourly})
            SELECT day, currency, SUM(hours) AS hours, SUM(cost) AS cost
            FROM src GROUP BY day, currency ORDER BY day
            """,
            limit=800,
        )

        # The same series, but per meter, so a filter can rebuild the charts from the rows it
        # kept. Fetched as a second pass rather than derived from the one above because the
        # estate totals must stay exact even when this is dropped for size.
        cells = r.rows(
            f"""
            WITH src AS ({hourly})
            SELECT id, meter, currency, day, SUM(hours) AS hours, SUM(cost) AS cost
            FROM src GROUP BY id, meter, currency, day
            HAVING SUM(hours) > 0
            """,
            limit=MAX_DAILY_CELLS + 1,
        )

    currency_code, mixed = _dominant(totals)
    if currency_code is None:
        return _empty(days)

    if mixed:
        rows = [x for x in rows if (x.get("currency") or "unknown") == currency_code]
        daily = [x for x in daily if (x.get("currency") or "unknown") == currency_code]
        cells = [x for x in cells if (x.get("currency") or "unknown") == currency_code]

    truncated = len(rows) > MAX_ROWS
    if truncated:
        log.info("hourly_cost: more than %d hour-billed meters; response is partial", MAX_ROWS)
        rows = rows[:MAX_ROWS]

    total_spend = sum(float(t["cost"] or 0.0) for t in totals
                      if not mixed or (t.get("currency") or "unknown") == currency_code)

    resources = [_shape(row) for row in rows]
    # Resource before meter: someone reading this wants the expensive machine, not the
    # expensive line item on it, and a VM's compute and licence meters are two rows here.
    resources.sort(key=lambda x: x["cost"], reverse=True)

    trend = _trend(daily)
    dates, per_meter, dropped = _cells(cells, {r["key"] for r in resources}, trend["labels"])
    return {
        "days": days,
        "latest": str(hi),
        "currency": currency_code,
        "mixed_currency": mixed,
        "truncated": truncated,
        "totals": _totals(resources, total_spend, trend),
        "resources": resources,
        "trend": trend,
        "weekday": _weekday(daily),
        "schedules": SCHEDULE_PRESETS,
        # What the client needs to answer the same questions about a subset: the day axis, and
        # each meter's hours and cost against it.
        "dates": dates,
        "daily": per_meter,
        "daily_dropped": dropped,
        "total_spend": round(total_spend, 4),
    }


def _cells(cells: list[dict[str, Any]], keep: set[str],
           labels: list[str]) -> tuple[list[str], dict[str, list[list[float]]], bool]:
    """Per-meter daily hours and cost, indexed against the trend's day axis.

    Positions rather than dates, and one array per meter rather than a row per cell: the client
    sums these on every filter change, and a list of objects carrying a repeated date string
    costs both bandwidth and a parse on each redraw.

    Days come from the data, so a day Azure never billed is absent rather than zero — a gap
    stays a gap, and drawing it as nothing spent would be a statement the data does not make.
    """
    if len(cells) > MAX_DAILY_CELLS:
        log.info("hourly_cost: over %d daily cells; dropping per-meter detail", MAX_DAILY_CELLS)
        return labels, {}, True

    index = {d: i for i, d in enumerate(labels)}
    out: dict[str, list[list[float]]] = {}
    for cell in cells:
        key = f"{cell.get('id')}\u0000{cell.get('meter')}"
        # A meter dropped by the row cap has no line in the table, so its cells would be
        # invisible weight in the payload and would push the "all" charts above the table
        # they are meant to describe.
        if key not in keep:
            continue
        position = index.get(str(cell.get("day")))
        if position is None:
            continue
        out.setdefault(key, []).append([
            position,
            round(float(cell.get("hours") or 0.0), 3),
            round(float(cell.get("cost") or 0.0), 6),
        ])
    return labels, out, False


# Presets the client models savings against. Hours are per week out of 168, which is what makes
# them comparable: "12x5" is 60 hours, so a resource left on all week is paying for 108 it is
# not using. Sent from here so the tab and any report agree on what "business hours" means.
SCHEDULE_PRESETS = [
    {"id": "business", "label": "Business hours (7am–7pm, Mon–Fri)", "hours": 60},
    {"id": "extended", "label": "Extended hours (6am–8pm, Mon–Fri)", "hours": 70},
    {"id": "weekdays", "label": "Weekdays only (24x5)", "hours": 120},
    {"id": "nights", "label": "Nights off (6am–10pm, every day)", "hours": 112},
]


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """One resource-meter, with the instance count and uptime recovered from its daily shape."""
    hours = float(row.get("hours") or 0.0)
    cost = float(row.get("cost") or 0.0)
    peak = float(row.get("peak_day_hours") or 0.0)

    # How many of this thing there are. A two-node pool bills 48 hours on a day it never
    # stopped, so the busiest day is what "fully on" costs — anything less than that peak is
    # time it was genuinely not running, and dividing by a flat 24 would report 200% uptime.
    units = max(1, round(peak / 24.0)) if peak > 0 else 1

    first, last = row.get("first_day"), row.get("last_day")
    span = _span_days(first, last)
    capacity = units * 24.0 * span
    uptime = min(1.0, hours / capacity) if capacity > 0 else 0.0

    service = str(row.get("service") or "")
    return {
        "id": row.get("id"),
        # Joins this row to its daily cells. A resource can bill several hourly meters — a VM's
        # compute and its licence are two lines here — so the resource id alone is not unique.
        # NUL separates because it cannot occur in either half.
        "key": f"{row.get('id')}\u0000{row.get('meter')}",
        "name": row.get("name"),
        "service": service,
        "subscription": row.get("subscription"),
        "resource_group": row.get("resource_group"),
        "region": row.get("region"),
        "meter": row.get("meter"),
        "hours": round(hours, 1),
        "cost": round(cost, 4),
        # The number the whole tab exists for: what one hour of this resource costs.
        "rate": round(cost / hours, 6) if hours > 0 else 0.0,
        "units": units,
        "span_days": span,
        "days_billed": int(row.get("days_billed") or 0),
        "uptime_pct": round(uptime * 100, 1),
        "always_on": uptime >= ALWAYS_ON,
        "schedulable": _schedulable(service),
        "first_day": str(first) if first else None,
        "last_day": str(last) if last else None,
    }


def _schedulable(service: str) -> bool:
    """Whether stopping this out of hours is a thing Azure actually lets you do.

    A managed search index, a Defender node or an Event Hubs throughput unit bills by the hour
    and cannot be switched off overnight, so counting their idle hours as a saving would put a
    number in front of someone that they can never bank.
    """
    low = service.lower()
    return any(word in low for word in SCHEDULABLE)


def _span_days(first: Any, last: Any) -> int:
    """Days between a resource's first and last billed day, inclusive.

    The window is the wrong denominator. A VM created last Tuesday was not "switched off" for
    the fortnight before it existed, and a decommissioned one is not still down — measuring
    across the observed lifetime keeps uptime a statement about the resource rather than about
    when the report was run.
    """
    try:
        return max(1, (last - first).days + 1)
    except (TypeError, AttributeError):
        return 1


def _trend(daily: list[dict[str, Any]]) -> dict[str, Any]:
    """Hours, cost and the effective rate, day by day."""
    by_day: dict[str, dict[str, float]] = {}
    for row in daily:
        day = str(row.get("day"))
        cell = by_day.setdefault(day, {"hours": 0.0, "cost": 0.0})
        cell["hours"] += float(row.get("hours") or 0.0)
        cell["cost"] += float(row.get("cost") or 0.0)

    labels = sorted(by_day)
    return {
        "labels": labels,
        "hours": [round(by_day[d]["hours"], 1) for d in labels],
        "cost": [round(by_day[d]["cost"], 4) for d in labels],
        # Cost per hour actually consumed. Flat means the estate is the same size; a step means
        # something was resized or a reservation started, which a cost line alone cannot tell
        # you apart from something simply running longer.
        "rate": [
            round(by_day[d]["cost"] / by_day[d]["hours"], 6) if by_day[d]["hours"] else 0.0
            for d in labels
        ],
    }


def _weekday(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average hours and cost per day of the week.

    This is the closest thing to a clock that daily billing supports, and unlike an invented
    hour-of-day curve it is measured: if the estate is smaller at weekends, the hours are
    genuinely not there. A flat profile is itself the answer — nothing is on a schedule.
    """
    buckets: dict[int, dict[str, float]] = {}
    for row in daily:
        day = row.get("day")
        try:
            index = day.weekday()
        except AttributeError:
            continue
        cell = buckets.setdefault(index, {"hours": 0.0, "cost": 0.0, "n": 0.0})
        cell["hours"] += float(row.get("hours") or 0.0)
        cell["cost"] += float(row.get("cost") or 0.0)
        cell["n"] += 1

    out = []
    for index, label in enumerate(WEEKDAYS):
        cell = buckets.get(index)
        n = cell["n"] if cell else 0
        out.append({
            "day": label,
            "short": label[:3],
            "days": int(n),
            "hours": round(cell["hours"] / n, 1) if n else 0.0,
            "cost": round(cell["cost"] / n, 4) if n else 0.0,
        })
    return out


def _totals(resources: list[dict[str, Any]], total_spend: float,
            trend: dict[str, Any]) -> dict[str, Any]:
    """The headline figures, all derived from the rows already computed."""
    hours = sum(r["hours"] for r in resources)
    cost = sum(r["cost"] for r in resources)
    always_on = [r for r in resources if r["always_on"]]
    idle_candidates = [r for r in resources if r["always_on"] and r["schedulable"]]

    # What the estate burns per hour right now: recent, so a resource deleted three weeks ago
    # does not drag it down, but not the last day alone. The newest day in a cost export is
    # almost always partial — this estate's final day reads 17.50 against a 29.00 run rate —
    # so taking it literally understates the burn by 40%. The median of the last week is
    # recent and immune to that one short day.
    return {
        "hours": round(hours, 1),
        "cost": round(cost, 4),
        "total_spend": round(total_spend, 4),
        "coverage_pct": round(cost / total_spend * 100, 1) if total_spend > 0 else 0.0,
        # The estate-wide blended rate: every hour-billed thing, averaged by spend.
        "avg_rate": round(cost / hours, 6) if hours > 0 else 0.0,
        "run_rate": round(_recent_daily(trend["cost"]) / 24.0, 6),
        "run_hours": round(_recent_daily(trend["hours"]), 1),
        "resources": len({r["id"] for r in resources}),
        "meters": len(resources),
        "always_on": len(always_on),
        "always_on_cost": round(sum(r["cost"] for r in always_on), 4),
        "schedulable": len(idle_candidates),
        "schedulable_cost": round(sum(r["cost"] for r in idle_candidates), 4),
    }


def _recent_daily(series: list[float]) -> float:
    """A typical recent day, resistant to the partial one at the end.

    Cost exports restate for a day or two, so the newest day is routinely a fraction of a real
    one. A mean would carry that straight into the headline; a median over the last week
    ignores it unless several days are short, in which case the estate genuinely did shrink.
    """
    tail = [float(v) for v in series[-7:]]
    if not tail:
        return 0.0
    tail.sort()
    mid = len(tail) // 2
    return tail[mid] if len(tail) % 2 else (tail[mid - 1] + tail[mid]) / 2


def _dominant(rows: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """The currency most of the money is in, and whether there was more than one.

    Adding EUR to USD produces a confident, meaningless number, so the estate is reported in
    the currency that dominates it and the tab says that is what it did.
    """
    totals: dict[str, float] = {}
    for row in rows:
        code = row.get("currency") or "unknown"
        totals[code] = totals.get(code, 0.0) + float(row.get("cost") or 0.0)
    if not totals:
        return None, False
    if len(totals) == 1:
        return next(iter(totals)), False
    return max(totals, key=lambda c: totals[c]), True


def _empty(days: int) -> dict[str, Any]:
    return {
        "days": days, "latest": None, "currency": None, "mixed_currency": False,
        "truncated": False, "resources": [], "weekday": [],
        "trend": {"labels": [], "hours": [], "cost": [], "rate": []},
        "schedules": SCHEDULE_PRESETS,
        "dates": [], "daily": {}, "daily_dropped": False, "total_spend": 0.0,
        "totals": {"hours": 0.0, "cost": 0.0, "total_spend": 0.0, "coverage_pct": 0.0,
                   "avg_rate": 0.0, "run_rate": 0.0, "run_hours": 0.0, "resources": 0,
                   "meters": 0, "always_on": 0, "always_on_cost": 0.0, "schedulable": 0,
                   "schedulable_cost": 0.0},
    }

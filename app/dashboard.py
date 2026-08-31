"""The dashboard behind the tabs: the same warehouse, arranged rather than asked.

The chat is good at questions nobody anticipated. It is a poor way to answer the four things
everyone wants the moment they open a cost tool — what am I spending, on what, where is it
going up, and what is idle. Those belong on a page you can read without typing.

Every figure here comes from the local warehouse, so a tab renders in milliseconds and the
model is not involved at all. The one exception is stale resources, which needs live inventory
from Azure because "this disk is attached to nothing" is not a fact the cost export knows.

Grouping follows Azure's own `ServiceFamily`, with one rule that matters for a cost tool:
**every currency unit lands in exactly one tab.** People add tabs up and expect the total, so
anything not claimed by a named group falls into "Other" rather than quietly disappearing.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cloudlens.dashboard")

# Curated groups. Ordered: this is the tab order, and it runs roughly from the infrastructure
# people recognise to the things they go looking for less often.
GROUPS: list[dict[str, Any]] = [
    {"id": "compute", "label": "Compute & Web",
     "families": ["Compute", "Web", "Containers"]},
    {"id": "networking", "label": "Networking",
     "families": ["Networking", "Bandwidth"]},
    {"id": "storage", "label": "Storage",
     "families": ["Storage"]},
    {"id": "data", "label": "AI & Data",
     "families": ["AI + Machine Learning", "Analytics", "Data", "Databases",
                  "Microsoft Fabric"]},
    {"id": "integration", "label": "Integration & IoT",
     "families": ["Integration", "Internet of Things", "Azure Communication Services",
                  "Mixed Reality", "Media"]},
    {"id": "security", "label": "Security & Ops",
     "families": ["Security", "Management and Governance", "Developer Tools", "Azure Arc",
                  "Migration", "Monitor"]},
]

CLAIMED = {f for g in GROUPS for f in g["families"]}
# Named "Other services" rather than "Other": in a rail where every neighbour names a service
# family, a bare "Other" reads as a leftovers bin rather than as the spend it actually holds.
OTHER = {"id": "other", "label": "Other services", "families": []}


def _q(value: str) -> str:
    """Single-quote a literal for SQL. Family names are ours, not user input, but a stray
    apostrophe in a future Azure service name should not become a syntax error."""
    return "'" + str(value).replace("'", "''") + "'"


def _families_clause(families: list[str], negate: bool = False) -> str:
    if not families:
        return "1=1"
    joined = ", ".join(_q(f) for f in families)
    return f'"ServiceFamily" {"NOT " if negate else ""}IN ({joined})'


class Dashboard:
    """Reads the warehouse. Holds no state: a tab is a set of queries, not a session."""

    def __init__(self) -> None:
        from .warehouse import warehouse

        self.wh = warehouse

    # ------------------------------------------------------------------ helpers
    def _rows(self, sql: str, scope: list[str] | None, limit: int = 200,
              currency: str | None = None) -> list[dict]:
        """Single query on its own connection. Prefer `reader` when asking several questions."""
        with self.wh.reader(scope, currency) as r:
            return r.rows(sql, limit)

    def _window(self, reader: Any, days: int) -> tuple[str, str, str]:
        """The period to report on, anchored to the newest data rather than to today.

        Cost data lands a day or two late; anchoring to `today` would show a partial or empty
        final day and make every trend look like it fell off a cliff.
        """
        rows = reader.rows(
            'SELECT max("ChargePeriodStart") AS hi, min("ChargePeriodStart") AS lo FROM costs')
        hi = rows[0]["hi"] if rows and rows[0]["hi"] else None
        if hi is None:
            return "", "", ""
        start = f"date '{hi}' - INTERVAL {int(days)} DAY"
        prev = f"date '{hi}' - INTERVAL {int(days) * 2} DAY"
        return str(hi), start, prev

    @staticmethod
    def _reduce(rows: list[dict], key: str = "cost") -> tuple[float, str | None, bool]:
        """Collapse per-currency rows into one number, or refuse to if the estate is mixed.

        Adding EUR to USD produces a confident, meaningless figure — the one number a cost
        dashboard must never show.
        """
        by_currency: dict[str, float] = {}
        for r in rows:
            cur = r.get("currency") or "unknown"
            by_currency[cur] = by_currency.get(cur, 0.0) + float(r.get(key) or 0.0)
        if not by_currency:
            return 0.0, None, False
        if len(by_currency) == 1:
            cur, total = next(iter(by_currency.items()))
            return round(total, 2), cur, False
        dominant = max(by_currency, key=lambda c: by_currency[c])
        return round(by_currency[dominant], 2), dominant, True

    # -------------------------------------------------------------------- tabs
    def sections(self, scope: list[str] | None, days: int = 30,
                 currency: str | None = None) -> dict[str, Any]:
        """The tab bar: which groups exist in this data, and what each is costing."""
        with self.wh.reader(scope, currency) as r:
            hi, start, _ = self._window(r, days)
            if not hi:
                return {"sections": [], "days": days, "empty": True}

            rows = r.rows(
                'SELECT "ServiceFamily" AS family, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f'WHERE "ChargePeriodStart" > {start} GROUP BY 1, 2', 500)

        seen = {r["family"] for r in rows if r["family"]}
        out: list[dict] = []
        for group in GROUPS:
            mine = [r for r in rows if r["family"] in group["families"]]
            total, currency, mixed = self._reduce(mine)
            out.append({"id": group["id"], "label": group["label"], "cost": total,
                        "currency": currency, "mixed": mixed})

        # Whatever no group claimed, so the tabs still add up to the total.
        leftover = [r for r in rows if r["family"] not in CLAIMED]
        total, currency, mixed = self._reduce(leftover)
        if total:
            out.append({"id": OTHER["id"], "label": OTHER["label"], "cost": total,
                        "currency": currency, "mixed": mixed})

        grand, currency, mixed = self._reduce(rows)
        return {"sections": out, "total": grand, "currency": currency, "mixed": mixed,
                "days": days, "as_of": hi,
                "unclaimed": sorted(f for f in seen if f not in CLAIMED)}

    def executive(self, scope: list[str] | None, days: int = 30,
                  currency: str | None = None) -> dict[str, Any]:
        """The opening view: what an owner of this estate needs before asking anything.

        Everything here is one burst against the local warehouse on a single connection, so a
        page that answers "what am I spending, on what, who is spending it, and what changed"
        costs milliseconds rather than a round of Azure calls.

        The comparisons are all against the *immediately preceding* window of the same length,
        anchored to the newest data rather than to today — cost lands a day or two late, so
        anchoring to today makes every trend look like it fell off a cliff on the last day.
        """
        from datetime import date, timedelta

        with self.wh.reader(scope, currency) as r:
            hi, start, prev = self._window(r, days)
            if not hi:
                return {"days": days, "empty": True}

            period = f'"ChargePeriodStart" > {start}'
            window = f'"ChargePeriodStart" > {prev}'
            # One pass per shape, all inside the same connection. Splitting current from
            # previous with a CASE rather than two queries halves the scans.
            split = (
                f'sum(CASE WHEN "ChargePeriodStart" > {start} THEN "BilledCost" ELSE 0 END) AS cost, '
                f'sum(CASE WHEN "ChargePeriodStart" <= {start} THEN "BilledCost" ELSE 0 END) AS prior'
            )

            totals = r.rows(
                'SELECT "BillingCurrency" AS currency, ' + split + f" FROM costs WHERE {window} GROUP BY 1",
                50)

            counts = r.rows(
                'SELECT count(DISTINCT "ResourceId") AS resources, '
                'count(DISTINCT "ServiceName") AS services, '
                'count(DISTINCT "SubAccountId") AS subscriptions, '
                'count(DISTINCT "RegionName") AS regions '
                f"FROM costs WHERE {period}", 5)

            daily = r.rows(
                'SELECT "ChargePeriodStart" AS day, "BillingCurrency" AS currency, '
                f'sum("BilledCost") AS cost FROM costs WHERE {window} GROUP BY 1, 2 ORDER BY 1',
                800)

            families = r.rows(
                'SELECT "ServiceFamily" AS family, "BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 500)

            services = r.rows(
                'SELECT "ServiceName" AS name, "BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 400)

            subs = r.rows(
                'SELECT coalesce("SubAccountName", "SubAccountId", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 200)

            groups_top = r.rows(
                'SELECT coalesce("ResourceGroup", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 400)

            regions_top = r.rows(
                'SELECT coalesce("RegionName", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + f' , count(DISTINCT "ResourceId") AS resources'
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 200)

        total, currency, mixed = self._reduce(totals)
        prior_total, _, _ = self._reduce(totals, "prior")
        c = counts[0] if counts else {}

        # The trend is split by date in Python: the boundary is a real date here, whereas in
        # SQL it was an expression, and the chart wants two aligned series anyway.
        edge = date.fromisoformat(str(hi)) - timedelta(days=int(days))
        cur_days: dict[str, float] = {}
        prev_days: dict[str, float] = {}
        for row in daily:
            d = date.fromisoformat(str(row["day"]))
            bucket = cur_days if d > edge else prev_days
            bucket[str(d)] = bucket.get(str(d), 0.0) + float(row["cost"] or 0.0)

        def movers(rows: list[dict], keys: tuple[str, ...] = ("name",)) -> list[dict]:
            """Merge per-currency rows, then describe the change rather than just the total."""
            merged: dict[tuple, dict] = {}
            for row in rows:
                k = tuple(row[key] for key in keys)
                item = merged.setdefault(k, {**{key: row[key] for key in keys},
                                             "cost": 0.0, "previous": 0.0})
                item["cost"] += float(row.get("cost") or 0.0)
                item["previous"] += float(row.get("prior") or 0.0)
            out = []
            for item in merged.values():
                item["cost"] = round(item["cost"], 2)
                item["previous"] = round(item["previous"], 2)
                item["delta"] = round(item["cost"] - item["previous"], 2)
                # None, not 0: "appeared this period" and "did not move" are different facts.
                item["change_pct"] = (round(item["delta"] / item["previous"] * 100, 1)
                                      if item["previous"] else None)
                item["share_pct"] = round(item["cost"] / total * 100, 1) if total else None
                out.append(item)
            return sorted(out, key=lambda x: -x["cost"])

        by_service = movers(services)
        by_sub = movers(subs)
        by_group = movers(groups_top)

        # Regions carry a resource count as well as cost, so the map can size by spend and the
        # table can still answer "how much is actually deployed there".
        by_region = movers(regions_top)
        counts_by_region: dict[str, int] = {}
        for row in regions_top:
            counts_by_region[row["name"]] = (
                counts_by_region.get(row["name"], 0) + int(row.get("resources") or 0)
            )
        for reg in by_region:
            reg["resources"] = counts_by_region.get(reg["name"], 0)

        # Areas, in the same order as the rail, so the two agree.
        family_rows = movers(families, ("family",))
        areas = []
        for group in GROUPS:
            mine = [f for f in family_rows if f["family"] in group["families"]]
            areas.append({
                "id": group["id"], "label": group["label"],
                "cost": round(sum(f["cost"] for f in mine), 2),
                "previous": round(sum(f["previous"] for f in mine), 2),
            })
        leftover = [f for f in family_rows if f["family"] not in CLAIMED]
        if leftover:
            areas.append({"id": OTHER["id"], "label": OTHER["label"],
                          "cost": round(sum(f["cost"] for f in leftover), 2),
                          "previous": round(sum(f["previous"] for f in leftover), 2)})
        for a in areas:
            a["delta"] = round(a["cost"] - a["previous"], 2)
            a["change_pct"] = (round(a["delta"] / a["previous"] * 100, 1)
                               if a["previous"] else None)
            a["share_pct"] = round(a["cost"] / total * 100, 1) if total else None
        areas = [a for a in areas if a["cost"]]

        # What actually moved, biggest absolute swing first. Ranking by percentage would put a
        # service that went from 2p to 8p above one that added a thousand pounds.
        swings = sorted((s for s in by_service if abs(s["delta"]) >= 0.01),
                        key=lambda x: -abs(x["delta"]))[:8]

        # How concentrated the bill is. A useful thing to know before optimising: if five
        # resources are most of the spend, that is where the effort goes.
        top5 = sum(s["cost"] for s in by_service[:5])

        # The most recent day of a cost export is usually still filling up: the exporter writes
        # it hours before the day is over. Plotted as-is it looks like spend collapsed, which on
        # the opening screen is the most misleading thing the page could say. Flag it here,
        # where the data is, rather than leaving every caller to guess.
        cur_values = [round(v, 2) for v in cur_days.values()]
        partial_last = False
        if len(cur_values) >= 5:
            # Compare the final day against the median of up to a week before it. A median
            # rather than a mean so one spike in the window does not mask a real partial day.
            recent = sorted(cur_values[max(0, len(cur_values) - 8):-1])
            median = recent[len(recent) // 2]
            partial_last = bool(median) and cur_values[-1] < median * 0.4

        return {
            "days": days,
            "as_of": str(hi),
            "currency": currency,
            "mixed_currency": mixed,
            "kpis": {
                "total": total,
                "previous": prior_total,
                "change_pct": (round((total - prior_total) / prior_total * 100, 1)
                               if prior_total else None),
                "daily_avg": round(total / days, 2) if days else None,
                "resources": int(c.get("resources") or 0),
                "services": int(c.get("services") or 0),
                "subscriptions": int(c.get("subscriptions") or 0),
                "regions": int(c.get("regions") or 0),
                "top5_share_pct": round(top5 / total * 100, 1) if total else None,
            },
            "trend": {
                "labels": list(cur_days),
                "values": cur_values,
                # Aligned by position, not by date: the point is "day 1 vs day 1", and the
                # previous window has its own dates that would clutter the axis.
                "previous": [round(v, 2) for v in prev_days.values()],
                # ...but the tooltip has to name both, or a reader sees one date above two
                # numbers and reasonably assumes both belong to it. The axis stays clean and the
                # hover carries the second date.
                "previous_labels": list(prev_days),
                "partial_last": partial_last,
            },
            "areas": areas,
            "movers": swings,
            "services": by_service[:8],
            "subscriptions": by_sub[:8],
            "resource_groups": by_group[:8],
            "regions": by_region[:20],
        }

    def section(self, section_id: str, scope: list[str] | None, days: int = 30,
                currency: str | None = None) -> dict[str, Any]:
        """Everything one tab needs, in four queries."""
        group = next((g for g in GROUPS if g["id"] == section_id), None)
        if group is None and section_id != "other":
            raise KeyError(section_id)

        if section_id == "other":
            where = _families_clause(sorted(CLAIMED), negate=True)
            label = "Other"
        else:
            where = _families_clause(group["families"])
            label = group["label"]

        hi = ""
        with self.wh.reader(scope, currency) as r:
            hi, start, prev = self._window(r, days)
            if not hi:
                return {"section": section_id, "label": label, "empty": True, "days": days}

            period = f'"ChargePeriodStart" > {start}'
            previous = f'"ChargePeriodStart" > {prev} AND "ChargePeriodStart" <= {start}'

            totals = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost, '
                'count(DISTINCT "ResourceId") AS resources FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1", 50)

            before = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {previous} GROUP BY 1", 50)

            everything = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f"WHERE {period} GROUP BY 1", 50)

            trend = r.rows(
                'SELECT "ChargePeriodStart" AS day, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2 ORDER BY 1", 400)

            services = r.rows(
                'SELECT "ServiceName" AS name, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2 ORDER BY cost DESC", 60)

            top = r.rows(
                'SELECT coalesce("ResourceName", \'—\') AS name, '
                'coalesce("ResourceGroup", \'—\') AS grp, "ServiceName" AS service, '
                'coalesce("RegionName", \'—\') AS region, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} AND \"ResourceId\" IS NOT NULL "
                "GROUP BY 1, 2, 3, 4, 5 ORDER BY cost DESC", 25)

            regions = r.rows(
                'SELECT coalesce("RegionName", \'—\') AS name, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2 ORDER BY cost DESC", 12)

        total, currency, mixed = self._reduce(totals)
        resources = sum(int(x.get("resources") or 0) for x in totals)
        prior, _, _ = self._reduce(before)
        grand, _, _ = self._reduce(everything)

        by_day: dict[str, float] = {}
        for row in trend:
            key = str(row["day"])
            by_day[key] = by_day.get(key, 0.0) + float(row["cost"] or 0.0)

        def flatten(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
            """Merge the per-currency rows a money query has to produce back into one row."""
            merged: dict[tuple, dict] = {}
            for r in rows:
                k = tuple(r[key] for key in keys)
                item = merged.setdefault(k, {**{key: r[key] for key in keys}, "cost": 0.0})
                item["cost"] += float(r["cost"] or 0.0)
            out = sorted(merged.values(), key=lambda x: -x["cost"])
            for item in out:
                item["cost"] = round(item["cost"], 2)
            return out

        return {
            "section": section_id,
            "label": label,
            "days": days,
            "as_of": hi,
            "currency": currency,
            "mixed_currency": mixed,
            "kpis": {
                "total": total,
                "previous": prior,
                # None rather than 0 when there is nothing to compare with: "no change" and
                # "no prior data" look identical otherwise, and only one of them is true.
                "change_pct": (round((total - prior) / prior * 100, 1) if prior else None),
                "share_pct": (round(total / grand * 100, 1) if grand else None),
                "resources": resources,
                "services": len({s["name"] for s in services}),
            },
            "trend": {"labels": list(by_day), "values": [round(v, 2) for v in by_day.values()]},
            "services": flatten(services, ("name",))[:12],
            "regions": flatten(regions, ("name",))[:8],
            "resources_top": flatten(top, ("name", "grp", "service", "region"))[:20],
        }


dashboard: Dashboard | None = None


def get_dashboard() -> Dashboard:
    global dashboard
    if dashboard is None:
        dashboard = Dashboard()
    return dashboard
